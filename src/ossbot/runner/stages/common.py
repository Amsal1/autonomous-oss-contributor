"""Helpers shared by the stages: sandbox job specs with fixed values, artifacts, issue text."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ossbot.common.config import ResolvedTarget, resolve_target
from ossbot.runner import prompts
from ossbot.runner.errors import sandbox_run
from ossbot.runner.llm import StageFailed, new_run_id
from ossbot.runner.pipeline import STAGE_TIMEOUT_S, StageContext

# Same pattern as sandbox/entrypoints/fetch.sh TEST_RE (fetch.json test_files / has_test_suite).
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|specs?|__tests__|testing)/|(^|/)test_[^/]*\.py$|_test\.[A-Za-z0-9]+$|\.(test|spec)\.[A-Za-z0-9]+$"
    r"|(^|/)(pytest\.ini|conftest\.py|jest\.config\.[A-Za-z]+|vitest\.config\.[A-Za-z]+|karma\.conf\.js"
    r"|phpunit\.xml(\.dist)?)$"
)


def has_test_suite(fetch_info: dict[str, Any] | None) -> bool:
    """The tests-required rule's "repo has a suite" (§9.14), from fetch.json; unknown = yes."""
    if not fetch_info or "has_test_suite" not in fetch_info:
        return True  # conservative: an old or missing fetch.json never disables the rule
    return bool(fetch_info["has_test_suite"])


def target(sc: StageContext, repo: str) -> ResolvedTarget:
    found = resolve_target(sc.config().targets, repo)
    if found is None:
        raise StageFailed("not_a_target", repo)
    return found


def job(
    sc: StageContext, cid: str, stage: str, profile: str, entrypoint: str, args: dict[str, Any], repo_mode: str,
    inputs: dict[str, str] | None = None, timeout_s: int | None = None,
) -> dict[str, Any]:  # fmt: skip
    """Run one sandbox job to its end; a failed job fails the stage with sandboxd's error class
    (an interrupted job or a sandboxd stop/outage interrupts it instead, ``errors.sandbox_run``)."""
    spec = {
        "run_id": new_run_id(stage), "cid": cid, "profile": profile, "entrypoint": entrypoint, "args": args,
        "repo_mode": repo_mode, "inputs": inputs or {}, "timeout_s": timeout_s or STAGE_TIMEOUT_S.get(stage, 1800),
    }  # fmt: skip
    done = sandbox_run(sc.sbx, spec, sc.tick)
    if done.get("state") != "succeeded":
        raise StageFailed(str(done.get("error_class") or f"{entrypoint}_failed"), str(spec["run_id"]))
    return done


def artifact(done: dict[str, Any], name: str) -> Path:
    base = done.get("artifact_dir")
    if not base:
        raise StageFailed("no_artifacts")
    path = Path(str(base)) / name
    if not path.is_file():
        raise StageFailed("artifact_missing", name)
    return path


def artifact_json(done: dict[str, Any], name: str) -> Any:
    try:
        return json.loads(artifact(done, name).read_text(encoding="utf-8"))
    except ValueError as exc:
        raise StageFailed("artifact_invalid", name) from exc


def write_inputs(sc: StageContext, cid: str, tag: str, files: dict[str, str | bytes]) -> dict[str, str]:
    base = sc.inputs_root / cid / new_run_id(tag)
    base.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for name, data in files.items():
        path = base / name
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")
        out[name] = str(path)
    return out


def issue_text(sc: StageContext, c: dict[str, Any]) -> tuple[str, str]:
    """Issue (or PR/repo for 👤 work) and its comments as untrusted prompt sections."""
    repo, number = str(c["upstream"]), c.get("issue_number")
    if number is None and c.get("work_ref") == "pull" and c.get("work_url"):
        number = int(str(c["work_url"]).rsplit("/", 1)[1])
    if number is None:
        return prompts.untrusted(f"github:{repo}", f"Repository {repo} (no issue: owner-directed work)"), ""
    issue = sc.gh.issue(repo, int(number)) or {}
    labels = ", ".join(str(lbl.get("name")) for lbl in issue.get("labels") or [] if isinstance(lbl, dict))
    body = (
        f"# {issue.get('title', '')}\nLabels: {labels}\nState: {issue.get('state', '?')}\n\n{issue.get('body') or ''}"
    )
    comments = "\n\n".join(
        f"## {(cm.get('user') or {}).get('login', '?')} at {cm.get('created_at', '')}\n{cm.get('body') or ''}"
        for cm in sc.gh.comments(repo, int(number))[-40:]
    )
    return (prompts.untrusted(f"github:{repo}#{number}", body),
            prompts.untrusted(f"github:{repo}#{number} comments", comments or "(no comments)"))  # fmt: skip


def owner_notes(c: dict[str, Any]) -> list[str]:
    notes = c.get("notes")
    return [prompts.human(f"The owner asked for this work: {notes}")] if notes else []
