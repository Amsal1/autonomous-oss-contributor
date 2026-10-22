"""Validated LLM outputs per role (DESIGN §9.5, §9.7, §9.8, §9.10, §9.11, §9.12; ``extra=forbid``).

The scout's ``Proposal`` is shared with the gatekeeper (``common.schemas.Proposal``).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from ossbot.common.ai_policy import ConditionKind

# One source for the limits the planner is told (SCHEMA_HINTS["planner"], prompts/planner.md; a test
# checks both name every number).
LINE_MAX = 300
SHORT_MAX = 500
PLAN_SUMMARY_MAX = 2000
PLAN_APPROACH_MAX = 20
PLAN_FILES_MAX = 100
PLAN_CONVENTIONS_MAX = 20
PLAN_CITATIONS_MAX = 30
PLAN_COMMANDS_MAX = 20
PLAN_SMOKE_MAX = 5
COMMAND_CMD_MAX = 1000
COMMAND_NAME_PATTERN = r"^[A-Za-z0-9_.-]{1,40}$"
VERDICT_SUMMARY_MAX = 20_000  # the salvage model's own bound (the stage cuts to SUMMARY_MAX)

Short = Annotated[str, Field(max_length=SHORT_MAX)]
Line = Annotated[str, Field(max_length=LINE_MAX)]


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(Out):
    source: Annotated[str, Field(max_length=200)]
    quote: Annotated[str, Field(max_length=500)]


class IssueVetting(Out):
    # untrusted claim, merged by common.ai_policy.merge_claim (a ban counts; an allow needs a doc quote)
    ai_policy: Literal["allowed", "disclosure_required", "banned", "unclear", "unstated"]
    ai_policy_evidence: Annotated[list[Evidence], Field(max_length=20)] = Field(default_factory=list)
    ai_conditions: Annotated[list[ConditionKind], Field(max_length=6)] = Field(default_factory=list)
    claim_status: Literal["unclaimed", "claimed_by_other", "claim_required", "stale_claim"]
    requires_claim_comment: bool = False
    requires_assignment: bool = False
    cla_required: bool = False
    dco_required: bool = False
    suitability: Annotated[float, Field(ge=0.0, le=1.0)]
    notes: Annotated[str, Field(max_length=2000)] = ""
    citations: Annotated[list[Short], Field(max_length=30)] = Field(default_factory=list)


class Command(Out):
    name: Annotated[str, Field(pattern=COMMAND_NAME_PATTERN)]
    cmd: Annotated[str, Field(min_length=1, max_length=COMMAND_CMD_MAX)]
    tier: Literal["static", "unit", "smoke", "integration"] = "unit"
    network: bool = False


class Plan(Out):
    feasible: bool
    summary: Annotated[str, Field(max_length=PLAN_SUMMARY_MAX)]
    approach: Annotated[list[Line], Field(max_length=PLAN_APPROACH_MAX)] = Field(default_factory=list)
    files_to_touch: Annotated[list[Line], Field(max_length=PLAN_FILES_MAX)] = Field(default_factory=list)
    risk: Literal["low", "medium", "high"] = "low"
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5
    validation_commands: Annotated[list[Command], Field(max_length=PLAN_COMMANDS_MAX)] = Field(default_factory=list)
    runtime_smoke: Annotated[list[Command], Field(max_length=PLAN_SMOKE_MAX)] = Field(default_factory=list)
    stack_validation: Literal["none"] = "none"  # compose/kind are phase 2 (§10.7)
    repo_conventions: Annotated[list[Line], Field(max_length=PLAN_CONVENTIONS_MAX)] = Field(default_factory=list)
    estimated_diff_lines: Annotated[int, Field(ge=0, le=100_000)] = 0
    citations: Annotated[list[Short], Field(max_length=PLAN_CITATIONS_MAX)] = Field(default_factory=list)


class InfeasibleVerdict(Out):
    """The two fields read from a planner answer that failed the ``Plan`` schema twice (§9.7): only a
    strict boolean ``feasible: false`` counts, and it can only stop the work (I8, I9)."""

    feasible: StrictBool
    summary: Annotated[StrictStr, Field(max_length=VERDICT_SUMMARY_MAX)] = ""


class CommandRun(Out):
    cmd: Annotated[str, Field(max_length=1000)]
    exit_code: int | None = None
    summary: Annotated[str, Field(max_length=500)] = ""


class ImplementReport(Out):
    files_changed: Annotated[list[Line], Field(max_length=200)] = Field(default_factory=list)
    tests_changed: Annotated[list[Line], Field(max_length=200)] = Field(default_factory=list)
    rationale: Annotated[str, Field(max_length=4000)] = ""
    commands_run: Annotated[list[CommandRun], Field(max_length=50)] = Field(default_factory=list)
    open_concerns: Annotated[list[Short], Field(max_length=20)] = Field(default_factory=list)
    needs_network: Annotated[list[Annotated[str, Field(pattern=r"^[A-Za-z0-9@/._:=<>~^ -]{1,200}$")]],
                             Field(max_length=20)] = Field(default_factory=list)  # fmt: skip
    needs_research: Annotated[list[Short], Field(max_length=10)] = Field(default_factory=list)


class Finding(Out):
    file: Annotated[str, Field(max_length=300)] = ""
    line: Annotated[int, Field(ge=0)] = 0
    issue: Annotated[str, Field(max_length=1000)]


class ReviewVerdict(Out):
    verdict: Literal["pass", "revise", "reject"]
    blocking: Annotated[list[Finding], Field(max_length=50)] = Field(default_factory=list)
    nits: Annotated[list[Short], Field(max_length=50)] = Field(default_factory=list)
    matches_repo_style: bool = True
    scope_creep: bool = False
    test_adequacy: Literal["adequate", "weak", "none"] = "adequate"
    tests_present: bool = True
    tests_exception: Literal["docs_only", "config_only", "no_suite"] | None = None
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5
    citations: Annotated[list[Short], Field(max_length=30)] = Field(default_factory=list)


class PrDraft(Out):
    commit_message: Annotated[str, Field(min_length=1, max_length=5000)]
    pr_title: Annotated[str, Field(min_length=1, max_length=256)]
    pr_body: Annotated[str, Field(max_length=50_000)] = ""


class WatchDecision(Out):
    action: Literal["reply", "fix", "rebase", "none"]
    reply_body: Annotated[str, Field(max_length=10_000)] = ""
    fix_request: Annotated[str, Field(max_length=4000)] = ""
    citations: Annotated[list[Short], Field(max_length=30)] = Field(default_factory=list)


SCHEMA_HINTS: dict[str, str] = {
    "vetter": '{"ai_policy": "...", "ai_conditions": [...], "claim_status": "...", "suitability": 0.0, ...}',
    "scout": '{"problem": "...", "approach": [...], "areas": [...], "kind": "bug", "estimate": {...}, ...}',
    "planner": (
        f'{{"feasible": true, "summary": "<= {PLAN_SUMMARY_MAX} chars",'
        f' "approach": ["<= {PLAN_APPROACH_MAX} items, each <= {LINE_MAX} chars"],'
        f' "files_to_touch": ["<= {PLAN_FILES_MAX} items, each <= {LINE_MAX} chars"],'
        f' "validation_commands": [<= {PLAN_COMMANDS_MAX} x {{"name": "{COMMAND_NAME_PATTERN}",'
        f' "cmd": "<= {COMMAND_CMD_MAX} chars", "tier": "unit"}}], "runtime_smoke": [<= {PLAN_SMOKE_MAX} commands],'
        f' "repo_conventions": ["<= {PLAN_CONVENTIONS_MAX} items, each <= {LINE_MAX} chars"],'
        f' "citations": ["<= {PLAN_CITATIONS_MAX} items, each <= {SHORT_MAX} chars"], ...}}'
        " With feasible: false, say why in summary."
    ),
    "coder": '{"files_changed": [...], "tests_changed": [...], "rationale": "...", ...}',
    "reviewer": '{"verdict": "pass|revise|reject", "blocking": [...], "tests_present": true, ...}',
    "summarizer": '{"commit_message": "...", "pr_title": "...", "pr_body": "..."}',
}
