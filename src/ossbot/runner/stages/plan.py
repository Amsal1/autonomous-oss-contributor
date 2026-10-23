"""Plan (DESIGN §9.7): the planner on the read-only snapshot, bound to the approved proposal.

FETCHED -> PLANNING -> PLANNED (evidence ``plan``) | ABANDONED. The runner rejects plans that are
infeasible, touch forbidden paths, or exceed the hard ceiling; ``risk: high`` is allowed only for
👤 items (the owner asked for it). The ABANDONED evidence carries the planner's ``summary`` (≤
``SUMMARY_MAX``), so the gatekeeper's terminal FYI can say why (§14.13.1).

An answer that fails the ``Plan`` schema twice but whose last fenced JSON says ``"feasible": false``
(a real boolean, read through ``InfeasibleVerdict``) is abandoned ``plan_infeasible`` with its summary
(``schema_salvaged: true``) instead of failing ``output_invalid``: the verdict can only stop the work.

A rerun with network (ADR-0024) reuses the stored, already checked plan once per rerun: FETCHED -> PLANNING ->
PLANNED with evidence ``{"plan": …, "reused": true}`` and no planner job (``reuse_plan``).
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from ossbot.common.size import path_matches
from ossbot.runner import output_parse, prompts
from ossbot.runner.errors import SUMMARY_MAX, OutputInvalidFailed
from ossbot.runner.outputs import SCHEMA_HINTS, InfeasibleVerdict, Plan
from ossbot.runner.pipeline import StageContext, StageRun
from ossbot.runner.stages.common import issue_text, owner_notes

STAGE = "plan"


def salvage_infeasible(text: str) -> InfeasibleVerdict | None:
    """``feasible: false`` (and the summary) from the last fenced JSON of an answer that failed the schema;
    None for anything else (no block, invalid JSON, not an object, a non-boolean or true ``feasible``)."""
    try:
        doc = output_parse.last_json_block(text)
    except output_parse.OutputInvalid:
        return None
    if not isinstance(doc, dict):
        return None
    try:
        verdict = InfeasibleVerdict.model_validate({k: doc[k] for k in ("feasible", "summary") if k in doc})
    except ValidationError:
        return None
    return verdict if verdict.feasible is False else None


def check(plan: Plan, *, forbidden: list[str], max_lines: int, user: bool) -> str | None:
    if not plan.feasible:
        return "plan_infeasible"
    for path in plan.files_to_touch:
        if path_matches(path, forbidden):
            return "forbidden_path"
    if plan.estimated_diff_lines > max_lines:
        return "plan_above_ceiling"
    if plan.risk == "high" and not user:
        return "plan_high_risk"
    return None


def reuse_plan(sc: StageContext, c: dict[str, Any]) -> bool:
    """ADR-0024: a rerun with network reuses the item's stored, already checked plan (no planner job): once per
    rerun (the note ``rerun_planned``), only when ``plan_json`` exists. True when it moved the item to PLANNED."""
    cid = str(c["id"])
    notes = sc.load(cid)
    seen = int(notes.get("rerun_seen") or 0)
    if seen <= 0 or not c.get("plan_json") or int(notes.get("rerun_planned") or 0) == seen:
        return False
    try:
        plan = json.loads(str(c["plan_json"]))
    except ValueError:
        return False
    if not isinstance(plan, dict):
        return False
    if c["state"] == "FETCHED":
        sc.transition(cid, "FETCHED", "PLANNING")
    sc.transition(cid, "PLANNING", "PLANNED", {"plan": plan, "reused": True})
    sc.save(cid, rerun_planned=seen)
    return True


def run(sc: StageContext, c: dict[str, Any]) -> None:
    cid = str(c["id"])
    if reuse_plan(sc, c):
        return
    if c["state"] == "FETCHED":
        sc.transition(cid, "FETCHED", "PLANNING")
    policy = sc.config().policy
    user = c.get("source") == "user"
    with StageRun(sc, cid, STAGE, profile="research") as run_:
        proposal = sc.gk.proposal(cid)
        issue, comments = issue_text(sc, c)
        fetch_info = sc.load(cid).get("fetch", {})
        sections = [
            issue, comments, *owner_notes(c),
            prompts.untrusted("approved proposal (scout output)", json.dumps(proposal["proposal"], indent=1)),
            prompts.untrusted("fetch.json", json.dumps(fetch_info)),
        ]  # fmt: skip
        if c.get("vetting_json"):
            sections.append(prompts.untrusted("vet facts", str(c["vetting_json"])))
        text = prompts.task("planner", f"Plan the approved change (class {c.get('approved_class')}).", sections,
                            SCHEMA_HINTS["planner"])  # fmt: skip
        salvaged: InfeasibleVerdict | None = None
        try:
            result = sc.kiro().invoke(cid, "planner", Plan, text, {"proposal.json": json.dumps(proposal["proposal"])},
                                      timeout_s=1200, schema_hint=SCHEMA_HINTS["planner"])  # fmt: skip
        except OutputInvalidFailed as exc:
            salvaged = salvage_infeasible(exc.last_text)
            if salvaged is None:
                raise  # FAILED output_invalid, as before
        else:
            run_.model_actual = result.model_actual
    if salvaged is not None:
        sc.transition(cid, "PLANNING", "ABANDONED", {"reason": "plan_infeasible", "schema_salvaged": True,
                                                     "summary": salvaged.summary[:SUMMARY_MAX]})  # fmt: skip
        return
    reason = check(result.value, forbidden=list(policy.diff.forbidden_paths), user=user,
                   max_lines=policy.diff.max_changed_lines + policy.diff.max_test_lines)  # fmt: skip
    if reason is not None:
        sc.transition(cid, "PLANNING", "ABANDONED", {"reason": reason, "summary": result.value.summary[:SUMMARY_MAX]})
        return
    sc.save(cid, models={**sc.load(cid).get("models", {}), "planner": result.model.model})
    sc.transition(cid, "PLANNING", "PLANNED", {"plan": result.value.model_dump()})
