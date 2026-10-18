"""Plan (DESIGN §9.7): the planner on the read-only snapshot, bound to the approved proposal.

FETCHED -> PLANNING -> PLANNED (evidence ``plan``) | ABANDONED. The runner rejects plans that are
infeasible, touch forbidden paths, or exceed the hard ceiling; ``risk: high`` is allowed only for
👤 items (the owner asked for it).
"""

from __future__ import annotations

import json
from typing import Any

from ossbot.common.size import path_matches
from ossbot.runner import prompts
from ossbot.runner.outputs import SCHEMA_HINTS, Plan
from ossbot.runner.pipeline import StageContext, StageRun
from ossbot.runner.stages.common import issue_text, owner_notes

STAGE = "plan"


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


def run(sc: StageContext, c: dict[str, Any]) -> None:
    cid = str(c["id"])
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
        result = sc.kiro().invoke(cid, "planner", Plan, text, {"proposal.json": json.dumps(proposal["proposal"])},
                                  timeout_s=1200, schema_hint=SCHEMA_HINTS["planner"])  # fmt: skip
        run_.model_actual = result.model_actual
    reason = check(result.value, forbidden=list(policy.diff.forbidden_paths), user=user,
                   max_lines=policy.diff.max_changed_lines + policy.diff.max_test_lines)  # fmt: skip
    if reason is not None:
        sc.transition(cid, "PLANNING", "ABANDONED", {"reason": reason})
        return
    sc.save(cid, models={**sc.load(cid).get("models", {}), "planner": result.model.model})
    sc.transition(cid, "PLANNING", "PLANNED", {"plan": result.value.model_dump()})
