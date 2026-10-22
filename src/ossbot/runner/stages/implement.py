"""Implement (DESIGN §9.8): the split coder (``agent-llm`` + ``agent-exec``, profile ``coder``).

Entered from PLANNED, VALIDATION_FAILED, REVISE, CHANGES_REQUESTED (the gatekeeper counts the
loops and refuses past the maxima) and from a WATCHING fix round, which first unbundles the last
approved bundle (``mode=fix_round``, args fixed from gatekeeper state). The coder adds tests that
cover the change (tests-required rule, §9.14). ``needs_network`` (a new dependency) fails the stage
with ``needs_network`` (v1 has no second fetch into a populated task volume; the owner is told by the
gatekeeper's terminal FYI, which shows the summary carried in the FAILED evidence).
"""

from __future__ import annotations

import json
from typing import Any

from ossbot.runner import prompts
from ossbot.runner.gk_client import GkError
from ossbot.runner.llm import StageFailed
from ossbot.runner.outputs import SCHEMA_HINTS, ImplementReport
from ossbot.runner.pipeline import StageContext, StageRun
from ossbot.runner.stages.common import issue_text, job, owner_notes, write_inputs

STAGE = "implement"
ENTRY = ("PLANNED", "VALIDATION_FAILED", "REVISE", "CHANGES_REQUESTED")
UNBUNDLE_TIMEOUT_S = 600  # the package profile allows at most 900 s (sandboxd PROFILE_MAX_TIMEOUT)


def _unbundle(sc: StageContext, c: dict[str, Any]) -> None:
    """Fix round on a live PR: the approved bundle back into the task volume (§9.8, M9)."""
    cid, rev = str(c["id"]), int(c["artifact_rev"])
    bundle = sc.inputs_root.parent.parent / "artifacts" / cid / str(rev) / "change.bundle"
    inputs = {"change.bundle": str(bundle)}
    signed = sc.gk.head_bundle(cid, rev)  # a signed head (ADR-0006 addendum): the gatekeeper's twin of it
    if signed is not None:
        inputs = write_inputs(sc, cid, STAGE, {"change.bundle": signed})
    args = {"mode": "fix_round", "bundle_ref": "refs/heads/ossbot-change", "checkout_sha": c["head_sha"], "rev": rev}
    job(sc, cid, "implement", "package", "unbundle", args, "rw", inputs=inputs,
        timeout_s=UNBUNDLE_TIMEOUT_S)  # fmt: skip


def feedback(sc: StageContext, c: dict[str, Any]) -> list[str]:
    notes = sc.load(str(c["id"]))
    out: list[str] = []
    if c["state"] == "VALIDATION_FAILED" and notes.get("validation"):
        failed = [cmd for cmd in notes["validation"].get("commands", []) if cmd.get("exit_code") != 0]
        out.append(prompts.untrusted("failed validation commands (tails)", json.dumps(failed, indent=1)[:30_000]))
    if c["state"] == "REVISE" and notes.get("review"):
        out.append(prompts.untrusted("reviewer findings", json.dumps(notes["review"].get("blocking", []))[:20_000]))
    for request in c.get("change_requests") or []:
        out.append(prompts.human(f"Change request from the owner: {request['text']}"))
    if notes.get("watch_fix"):
        out.append(prompts.untrusted("maintainer feedback to address", str(notes["watch_fix"])[:20_000]))
    return out


def run(sc: StageContext, c: dict[str, Any]) -> None:
    cid, state = str(c["id"]), str(c["state"])
    if state in ENTRY:
        try:
            sc.transition(cid, state, "IMPLEMENTING")
        except GkError as exc:
            if exc.code.startswith("guard_failed:"):  # a loop maximum: the item fails (WATCHING with a PR)
                sc.transition(cid, state, "FAILED", {"error_class": exc.code.split(":", 1)[1]})
                return
            raise
    plan = json.loads(c["plan_json"]) if c.get("plan_json") else {}
    with StageRun(sc, cid, STAGE, profile="coder", coder_mode="split") as run_:
        if c.get("pr_number") is not None and sc.load(cid).get("watch_fix"):
            _unbundle(sc, c)
        issue, _ = issue_text(sc, c)
        sections = [issue, *owner_notes(c), prompts.untrusted("approved plan", json.dumps(plan, indent=1)),
                    *feedback(sc, c)]  # fmt: skip
        text = prompts.task("coder", "Implement the plan with tests; leave the changes in the working tree.",
                            sections, SCHEMA_HINTS["coder"])  # fmt: skip
        result = sc.kiro().invoke(
            cid, "coder", ImplementReport, text, timeout_s=3600, schema_hint=SCHEMA_HINTS["coder"]
        )
        run_.model_actual = result.model_actual
        report = result.value
        if report.needs_network:  # the gatekeeper's terminal FYI carries the summary (§14.13.1)
            deps = ", ".join(report.needs_network[:5])
            summary = f"The change needs new dependencies ({deps}); v1 does not re-fetch, so it stops here."
            raise StageFailed("needs_network", summary=summary)
    models = {**sc.load(cid).get("models", {}), "coder": result.model.model}
    sc.save(cid, implement=report.model_dump(), models=models, watch_fix=None)
    sc.transition(cid, "IMPLEMENTING", "IMPLEMENTED",
                  {"files": report.files_changed[:50], "tests": report.tests_changed[:50]})  # fmt: skip
