"""Package (DESIGN §9.11): summarizer draft, the deterministic package job, ``POST /package``.

REVIEW_PASSED -> PACKAGING; the gatekeeper verifies everything itself (§12.5) and moves the item
to AWAITING_APPROVAL (or RESCOPE_REQUIRED for the ceiling / ``reapprove``, or keeps it in PACKAGING
awaiting a live claim). The PR body is the summarizer text + ``Fixes #N`` + the validation summary +
the repo's AI-policy checklist (only when vet recorded conditions; a title tag goes into the title) +
the exact disclosure block (§12.3). ``package_ts`` is fixed per rev, so retries give the same SHA.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from ossbot.common import ai_policy
from ossbot.common.ai_policy import AiCondition
from ossbot.common.project import PROJECT_NAME, PROJECT_URL
from ossbot.runner import prompts
from ossbot.runner.gk_client import GkError
from ossbot.runner.llm import StageFailed
from ossbot.runner.outputs import SCHEMA_HINTS, PrDraft
from ossbot.runner.pipeline import StageContext, StageRun
from ossbot.runner.stages.common import artifact, artifact_json, job, write_inputs

STAGE = "package"
ARTIFACTS = ("change.bundle", "change.patch", "diffstat.json")


def validation_summary(validation: dict[str, Any]) -> str:
    cmds = validation.get("commands", [])
    if not cmds:
        return "no local commands (relies on upstream CI)"
    passed = sum(1 for cmd in cmds if cmd.get("exit_code") == 0)
    return f"{passed}/{len(cmds)} commands passed ({', '.join(str(cmd.get('name')) for cmd in cmds[:8])})"


def ai_conditions(c: dict[str, Any]) -> tuple[AiCondition, ...]:
    """The repo's AI-policy conditions vet recorded (§9.5); invalid entries are dropped."""
    try:
        vetting = json.loads(c.get("vetting_json") or "{}")
    except ValueError:
        return ()
    return ai_policy.parse_conditions(vetting.get("ai_conditions")) if isinstance(vetting, dict) else ()


def pr_title(draft: PrDraft, conditions: Sequence[AiCondition] = ()) -> str:
    return ai_policy.apply_title_tag(draft.pr_title.strip()[:256], conditions)


def pr_body(draft: PrDraft, c: dict[str, Any], *, validation: dict[str, Any], models: dict[str, str],
            login: str, conditions: Sequence[AiCondition] = (), project_url: str = PROJECT_URL) -> str:  # fmt: skip
    lines = [draft.pr_body.strip()]
    if c.get("issue_number"):
        lines.append(f"Fixes #{c['issue_number']}")
    lines.append("### How this was validated\n" + "\n".join(
        f"- `{cmd.get('name')}`: exit {cmd.get('exit_code')}" for cmd in validation.get("commands", [])[:20]
    ) or "- relies on upstream CI")  # fmt: skip
    # the repo's AI-policy conditions, as fixed sentences (the gatekeeper checks them at /package)
    lines.append(ai_policy.checklist_block(conditions, login=login, coder_model=models.get("coder", "?"),
                                           reviewer_model=models.get("reviewer", "?")))  # fmt: skip
    # the one-line footer names no tool or model; a repo that requires that gets the tool_named checklist line
    footer = prompts.disclosure(project_url=project_url, project_name=PROJECT_NAME, github_login=login,
                                validation_summary=validation_summary(validation), cid=c["id"])  # fmt: skip
    lines.append(footer.strip())
    return "\n\n".join(part for part in lines if part)[:60_000]


def run(sc: StageContext, c: dict[str, Any]) -> None:
    cid = str(c["id"])
    if c["state"] == "REVIEW_PASSED":
        sc.transition(cid, "REVIEW_PASSED", "PACKAGING")
    notes = sc.load(cid)
    profile = sc.config().profile
    rev = int(c.get("artifact_rev") or 0) + 1
    stored = notes.get("package_body")
    if c.get("state_reason") == "awaiting_live_claim" and stored:  # N5: same rev once the claim is live
        _post(sc, c, stored)
        return
    if isinstance(stored, dict) and stored.get("rev") == rev:  # back from RESCOPE_REQUIRED: nothing is rebuilt
        _post(sc, c, stored)
        return
    fix_round = c.get("pr_number") is not None
    round_base = str(c["head_sha"] if fix_round else c["base_sha"])
    with StageRun(sc, cid, STAGE, profile="package") as run_:
        diff = str(notes.get("diff", ""))
        text = prompts.task(
            "summarizer",
            "Draft the commit message and PR text for this change.",
            [prompts.untrusted("diff", diff[:200_000] or "(empty)")],
            SCHEMA_HINTS["summarizer"],
        )
        result = sc.kiro().invoke(cid, "summarizer", PrDraft, text, {"change.diff": diff}, timeout_s=600,
                                  schema_hint=SCHEMA_HINTS["summarizer"])  # fmt: skip
        run_.model_actual = result.model_actual
        draft = result.value
        package_ts = sc.gk.package_ts(cid, rev)
        inputs = write_inputs(sc, cid, STAGE, {"commit_message.txt": draft.commit_message.strip() + "\n"})
        args = {"rev": rev, "round_base": round_base, "package_ts": package_ts, "name": profile.commit_name,
                "email": profile.commit_email}  # fmt: skip
        done = job(sc, cid, STAGE, "package", "package", args, "rw", inputs=inputs, timeout_s=600)
        outcome = artifact_json(done, "result.json")
        sums = {name: hashlib.sha256(artifact(done, name).read_bytes()).hexdigest() for name in ARTIFACTS}
    models = {**notes.get("models", {})}
    body: dict[str, Any] = {
        "rev": rev, "artifact_dir": str(done["artifact_dir"]), "sha256s": sums,
        "claimed_head_sha": str(outcome.get("head_sha")), "base_ref": str(c.get("base_ref") or "main"),
        "base_sha": str(c["base_sha"]), "validation": notes.get("validation", {}), "review": notes.get("review", {}),
    }  # fmt: skip
    if not fix_round:
        conditions = ai_conditions(c)
        body.update(
            pr_title=pr_title(draft, conditions),
            pr_body=pr_body(draft, c, validation=notes.get("validation", {}), models=models,
                            login=profile.github_login, conditions=conditions,
                            project_url=sc.config().policy.text.project_url),
        )  # fmt: skip
    sc.save(cid, package_body=body)
    _post(sc, c, body)


def _post(sc: StageContext, c: dict[str, Any], body: dict[str, Any]) -> None:
    cid = str(c["id"])
    try:
        answer = sc.gk.package(cid, body)
    except GkError as exc:
        if exc.status == 409:
            return  # not PACKAGING any more (the gatekeeper moved it, e.g. FAILED on a patch mismatch)
        raise StageFailed(f"package_{exc.code}"[:64], json.dumps(exc.detail)[:200]) from exc
    sc.save(cid, packaged={"rev": body["rev"], "answer": answer})
