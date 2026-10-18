"""Validated LLM outputs per role (DESIGN §9.5, §9.7, §9.8, §9.10, §9.11, §9.12; ``extra=forbid``).

The scout's ``Proposal`` is shared with the gatekeeper (``common.schemas.Proposal``).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Short = Annotated[str, Field(max_length=500)]
Line = Annotated[str, Field(max_length=300)]


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(Out):
    source: Annotated[str, Field(max_length=200)]
    quote: Annotated[str, Field(max_length=500)]


class IssueVetting(Out):
    ai_policy: Literal["allowed", "disclosure_required", "banned", "unclear"]
    ai_policy_evidence: Annotated[list[Evidence], Field(max_length=20)] = Field(default_factory=list)
    claim_status: Literal["unclaimed", "claimed_by_other", "claim_required", "stale_claim"]
    requires_claim_comment: bool = False
    requires_assignment: bool = False
    cla_required: bool = False
    dco_required: bool = False
    suitability: Annotated[float, Field(ge=0.0, le=1.0)]
    notes: Annotated[str, Field(max_length=2000)] = ""
    citations: Annotated[list[Short], Field(max_length=30)] = Field(default_factory=list)


class Command(Out):
    name: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,40}$")]
    cmd: Annotated[str, Field(min_length=1, max_length=1000)]
    tier: Literal["static", "unit", "smoke", "integration"] = "unit"
    network: bool = False


class Plan(Out):
    feasible: bool
    summary: Annotated[str, Field(max_length=2000)]
    approach: Annotated[list[Line], Field(max_length=20)] = Field(default_factory=list)
    files_to_touch: Annotated[list[Line], Field(max_length=100)] = Field(default_factory=list)
    risk: Literal["low", "medium", "high"] = "low"
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5
    validation_commands: Annotated[list[Command], Field(max_length=20)] = Field(default_factory=list)
    runtime_smoke: Annotated[list[Command], Field(max_length=5)] = Field(default_factory=list)
    stack_validation: Literal["none"] = "none"  # compose/kind are phase 2 (§10.7)
    repo_conventions: Annotated[list[Line], Field(max_length=20)] = Field(default_factory=list)
    estimated_diff_lines: Annotated[int, Field(ge=0, le=100_000)] = 0
    citations: Annotated[list[Short], Field(max_length=30)] = Field(default_factory=list)


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
    "vetter": '{"ai_policy": "...", "claim_status": "...", "suitability": 0.0, ...}',
    "scout": '{"problem": "...", "approach": [...], "areas": [...], "kind": "bug", "estimate": {...}, ...}',
    "planner": '{"feasible": true, "summary": "...", "validation_commands": [...], ...}',
    "coder": '{"files_changed": [...], "tests_changed": [...], "rationale": "...", ...}',
    "reviewer": '{"verdict": "pass|revise|reject", "blocking": [...], "tests_present": true, ...}',
    "summarizer": '{"commit_message": "...", "pr_title": "...", "pr_body": "..."}',
}
