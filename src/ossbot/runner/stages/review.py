"""Self-review (DESIGN §9.10, §9.14 tests-required rule).

VALIDATED -> REVIEWING -> REVIEW_PASSED | REVISE | REVIEW_REJECTED. Separate Kiro session, by default
a model distinct from the coder's (``require_distinct_from``). The tests rule is also enforced
deterministically: when the repo has a test suite and the change touches no test path, a "pass"
becomes a self-review rejection of this round (``revise`` while review rounds remain, else
``reject``) unless the change is docs-only or config-only. The reason is kept for the card.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ossbot.common.size import path_matches
from ossbot.runner import prompts
from ossbot.runner.outputs import SCHEMA_HINTS, Finding, ReviewVerdict
from ossbot.runner.pipeline import StageContext, StageRun
from ossbot.runner.stages.common import issue_text, owner_notes

STAGE = "review"
DIFF_PATH_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)
DOC_GLOBS = ("*.md", "*.rst", "*.txt", "*.adoc", "docs/**", "doc/**", "LICENSE*", "CHANGELOG*")
CONFIG_GLOBS = ("*.yml", "*.yaml", "*.toml", "*.ini", "*.cfg", "*.json", ".github/**", "Dockerfile", ".*rc")
TESTS_MISSING = "tests missing: the repo has a test suite and the change adds or updates no test"


def changed_paths(diff: str) -> list[str]:
    return sorted({m.group(2) for m in DIFF_PATH_RE.finditer(diff)})


def tests_exception(paths: list[str], has_suite: bool) -> str | None:
    if not has_suite:
        return "no_suite"
    if paths and all(path_matches(p, DOC_GLOBS) for p in paths):
        return "docs_only"
    if paths and all(path_matches(p, DOC_GLOBS) or path_matches(p, CONFIG_GLOBS) for p in paths):
        return "config_only"
    return None


def enforce_tests(verdict: ReviewVerdict, paths: list[str], *, test_globs: list[str], has_suite: bool,
                  rounds_left: bool) -> ReviewVerdict:  # fmt: skip
    """The deterministic backstop of the tests-required rule (AC 41)."""
    exception = tests_exception(paths, has_suite)
    touched = any(path_matches(p, test_globs) for p in paths)
    if touched or exception is not None or verdict.verdict == "reject":
        return verdict.model_copy(update={"tests_present": touched, "tests_exception": exception})
    blocking = [*verdict.blocking, Finding(issue=TESTS_MISSING)]
    return verdict.model_copy(update={"verdict": "revise" if rounds_left else "reject", "blocking": blocking,
                                      "tests_present": False, "test_adequacy": "none"})  # fmt: skip


def run(sc: StageContext, c: dict[str, Any]) -> None:
    cid = str(c["id"])
    if c["state"] == "VALIDATED":
        sc.transition(cid, "VALIDATED", "REVIEWING")
    notes = sc.load(cid)
    policy = sc.config().policy
    diff = str(notes.get("diff", ""))
    plan = json.loads(c["plan_json"]) if c.get("plan_json") else {}
    with StageRun(sc, cid, STAGE, profile="research") as run_:
        issue, _ = issue_text(sc, c)
        sections = [issue, *owner_notes(c),
                    prompts.untrusted("diff vs round base", diff[:200_000] or "(empty)"),
                    prompts.untrusted("plan", json.dumps(plan)[:40_000]),
                    prompts.untrusted("validation", json.dumps(notes.get("validation", {}))[:40_000])]  # fmt: skip
        text = prompts.task("reviewer", "Review the change.", sections, SCHEMA_HINTS["reviewer"])
        result = sc.kiro().invoke(cid, "reviewer", ReviewVerdict, text, {"change.diff": diff},
                                  timeout_s=1200, schema_hint=SCHEMA_HINTS["reviewer"])  # fmt: skip
        run_.model_actual = result.model_actual
    try:
        has_suite = bool(sc.gk.proposal(cid)["proposal"].get("tests"))
    except Exception:  # noqa: BLE001 - operator imports have no proposal: assume a suite
        has_suite = True
    verdict = enforce_tests(result.value, changed_paths(diff), test_globs=list(policy.size.test_globs),
                            has_suite=has_suite, rounds_left=int(c.get("review_rounds") or 0) <
                            policy.limits.max_review_rounds)  # fmt: skip
    models = {**notes.get("models", {}), "reviewer": result.model.model}
    sc.save(cid, review=verdict.model_dump(), models=models)
    to = {"pass": "REVIEW_PASSED", "revise": "REVISE", "reject": "REVIEW_REJECTED"}[verdict.verdict]
    sc.transition(cid, "REVIEWING", to, {"verdict": verdict.verdict, "tests_present": verdict.tests_present,
                                         "tests_exception": verdict.tests_exception})  # fmt: skip
    if to == "REVIEW_REJECTED":
        sc.transition(cid, "REVIEW_REJECTED", "ABANDONED", {"reason": "review_rejected"})
        sc.say(f"🔍 {c['upstream']}: self-review rejected the change ({len(verdict.blocking)} blocking).")
