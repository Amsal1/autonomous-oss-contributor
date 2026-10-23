"""Helpers shared by the stages: sandbox job specs with fixed values, artifacts, issue text."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ossbot.common.config import ResolvedTarget, resolve_target
from ossbot.common.schemas import ResolvedTargetView
from ossbot.runner import prompts
from ossbot.runner.errors import sandbox_run
from ossbot.runner.gk_client import GkError
from ossbot.runner.llm import StageFailed, new_run_id
from ossbot.runner.pipeline import STAGE_TIMEOUT_S, StageContext
from ossbot.runner.sbx_client import SbxError

# Same pattern as sandbox/entrypoints/fetch.sh TEST_RE (fetch.json test_files / has_test_suite).
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|specs?|__tests__|testing)/|(^|/)test_[^/]*\.py$|_test\.[A-Za-z0-9]+$|\.(test|spec)\.[A-Za-z0-9]+$"
    r"|(^|/)(pytest\.ini|conftest\.py|jest\.config\.[A-Za-z]+|vitest\.config\.[A-Za-z]+|karma\.conf\.js"
    r"|phpunit\.xml(\.dist)?)$"
)


log = logging.getLogger(__name__)
# repo-code jobs: sandboxd requires their egress (coder: agent-exec's network follows implement_network, ADR-0024)
EGRESS_JOB_PROFILES = frozenset({"fetch", "validate", "coder"})


def note_volume(sc: StageContext, cid: str, present: bool) -> None:
    """A cid whose sandbox resources (task volume, per-task networks) may exist (StageContext note)."""
    sc.note_sandbox(cid, present)


def volume_cids(sc: StageContext) -> list[str]:
    return sc.sandbox_cids()


def destroy_volume(sc: StageContext, cid: str) -> bool:
    """``POST /v1/tasks/{cid}/destroy`` (sandboxd allows it only for terminal or pre-Start-work
    items); best effort, the scheduler's sweep retries."""
    try:
        sc.sbx.destroy(cid)
    except SbxError as exc:
        if exc.status != 404:
            log.warning("task volume of %s not destroyed: %s", cid, exc.code, extra={"event": "destroy_failed"})
            return False
    note_volume(sc, cid, False)
    return True


def has_test_suite(fetch_info: dict[str, Any] | None) -> bool:
    """The tests-required rule's "repo has a suite" (§9.14), from fetch.json; unknown = yes."""
    if not fetch_info or "has_test_suite" not in fetch_info:
        return True  # conservative: an old or missing fetch.json never disables the rule
    return bool(fetch_info["has_test_suite"])


def target(sc: StageContext, repo: str) -> ResolvedTarget:
    """The config target (no call), else the gatekeeper's resolution of a discovered repo (design
    §8.5, ``GET /v1/targets/resolve``): 404 -> ``not_a_target``; an unreachable gatekeeper raises
    the usual ``GkError(503)``."""
    found = resolve_target(sc.config().targets, repo)
    if found is not None:
        return found
    try:
        doc = sc.gk.resolve_target(repo)
    except GkError as exc:
        if exc.status == 404:
            raise StageFailed("not_a_target", repo) from exc
        raise
    try:
        return ResolvedTargetView.model_validate(doc["target"]).to_target()
    except (KeyError, TypeError, ValidationError) as exc:
        raise StageFailed("not_a_target", f"{repo}: unreadable resolution") from exc


def job(
    sc: StageContext, cid: str, stage: str, profile: str, entrypoint: str, args: dict[str, Any], repo_mode: str,
    inputs: dict[str, str] | None = None, timeout_s: int | None = None, *, egress: str | None = None,
) -> dict[str, Any]:  # fmt: skip
    """Run one sandbox job to its end; a failed job fails the stage with sandboxd's error class
    (an interrupted job or a sandboxd stop/outage interrupts it instead, ``errors.sandbox_run``).

    ``egress`` (the target's ``ResolvedTarget.egress``: ``mode`` | ``strict``) is required for fetch and
    validate jobs and never defaulted (open-discovery design §12.3, fail closed); sandboxd refuses it
    for any other profile."""
    if profile in EGRESS_JOB_PROFILES and egress is None:
        log.error("egress_required", extra={"cid": cid, "stage": stage, "profile": profile})
        raise StageFailed("egress_required", profile)  # a runner bug: no repo-code job without a decision
    spec = {
        "run_id": new_run_id(stage), "cid": cid, "profile": profile, "entrypoint": entrypoint, "args": args,
        "repo_mode": repo_mode, "inputs": inputs or {}, "timeout_s": timeout_s or STAGE_TIMEOUT_S.get(stage, 1800),
        **({"egress": egress} if egress is not None else {}),
    }  # fmt: skip
    note_volume(sc, cid, True)  # task volume and/or per-task networks: the sweep destroys them later
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


def fetch_issue(sc: StageContext, c: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]], int | None]:
    """The raw issue (or PR for 👤 work), its last 40 comments and its number; (None, [], None) without one."""
    repo, number = str(c["upstream"]), c.get("issue_number")
    if number is None and c.get("work_ref") == "pull" and c.get("work_url"):
        number = int(str(c["work_url"]).rsplit("/", 1)[1])
    if number is None:
        return None, [], None
    issue = sc.gh.issue(repo, int(number)) or {}
    return issue, sc.gh.comments(repo, int(number))[-40:], int(number)


def render_issue(repo: str, number: int | None, issue: dict[str, Any] | None,
                 comments: list[dict[str, Any]]) -> tuple[str, str]:  # fmt: skip
    """Issue and comments as untrusted prompt sections."""
    if number is None:
        return prompts.untrusted(f"github:{repo}", f"Repository {repo} (no issue: owner-directed work)"), ""
    issue = issue or {}
    labels = ", ".join(str(lbl.get("name")) for lbl in issue.get("labels") or [] if isinstance(lbl, dict))
    body = (
        f"# {issue.get('title', '')}\nLabels: {labels}\nState: {issue.get('state', '?')}\n\n{issue.get('body') or ''}"
    )
    text = "\n\n".join(
        f"## {(cm.get('user') or {}).get('login', '?')} at {cm.get('created_at', '')}\n{cm.get('body') or ''}"
        for cm in comments
    )
    return (prompts.untrusted(f"github:{repo}#{number}", body),
            prompts.untrusted(f"github:{repo}#{number} comments", text or "(no comments)"))  # fmt: skip


def issue_text(sc: StageContext, c: dict[str, Any]) -> tuple[str, str]:
    """Issue (or PR/repo for 👤 work) and its comments as untrusted prompt sections."""
    issue, comments, number = fetch_issue(sc, c)
    return render_issue(str(c["upstream"]), number, issue, comments)


def owner_notes(c: dict[str, Any]) -> list[str]:
    notes = c.get("notes")
    return [prompts.human(f"The owner asked for this work: {notes}")] if notes else []
