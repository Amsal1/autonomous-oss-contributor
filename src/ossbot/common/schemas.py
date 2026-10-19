"""Shared enums, constants and gatekeeper API models (``extra="forbid"`` everywhere).

The stage/entrypoint constants are the single source shared by the runner, sandboxd, the
Docker filter and ``tests/unit/gatekeeper/test_control.py`` (AC 10), so later components extend
the same kill/pause parametrization.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ossbot.common.config import MODEL_ID_RE, REPO_RE, Effort


class State(StrEnum):
    DISCOVERED = "DISCOVERED"
    VETTING = "VETTING"
    VETTED = "VETTED"
    SKIPPED = "SKIPPED"
    IMPORTED = "IMPORTED"
    FETCHING = "FETCHING"
    FETCHED = "FETCHED"
    PLANNING = "PLANNING"
    PLANNED = "PLANNED"
    IMPLEMENTING = "IMPLEMENTING"
    IMPLEMENTED = "IMPLEMENTED"
    VALIDATING = "VALIDATING"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    VALIDATED = "VALIDATED"
    REVIEWING = "REVIEWING"
    REVISE = "REVISE"
    REVIEW_REJECTED = "REVIEW_REJECTED"
    REVIEW_PASSED = "REVIEW_PASSED"
    PACKAGING = "PACKAGING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    APPROVED = "APPROVED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    SUBMIT_FAILED = "SUBMIT_FAILED"
    DRY_RUN_DONE = "DRY_RUN_DONE"
    WATCHING = "WATCHING"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    ABANDONED = "ABANDONED"
    FAILED = "FAILED"
    MERGED = "MERGED"
    CLOSED = "CLOSED"
    # CR-004 (DESIGN §9.2): scout, gate 1 and re-scope
    SCOUTING = "SCOUTING"
    PROPOSED = "PROPOSED"
    PROPOSAL_APPROVED = "PROPOSAL_APPROVED"
    PROPOSAL_REJECTED = "PROPOSAL_REJECTED"
    PROPOSAL_SNOOZED = "PROPOSAL_SNOOZED"
    PROPOSAL_EXPIRED = "PROPOSAL_EXPIRED"
    RESCOPE_REQUIRED = "RESCOPE_REQUIRED"


TERMINAL_STATES: frozenset[State] = frozenset(
    {
        State.PROPOSAL_REJECTED,  # terminal unless the human Undo (Skip only, 10 min)
        State.SKIPPED,
        State.ABANDONED,
        State.REJECTED,
        State.EXPIRED,
        State.FAILED,
        State.DRY_RUN_DONE,
        State.MERGED,
        State.CLOSED,
    }
)

OutboundKind = Literal["open_pr", "push_update", "comment", "issue_claim"]
OUTBOUND_KINDS: tuple[OutboundKind, ...] = ("open_pr", "push_update", "comment", "issue_claim")
ContributionSource = Literal["discover", "hermes_request", "operator", "user"]  # 'user' = /work (👤)
ControlStateName = Literal["running", "paused", "killed"]

# Runner stage entrypoints (§9.1), sandboxd job entrypoints (sandbox/entrypoints/*.sh), the
# Docker filter operations refused while paused/killed (N5), and the gatekeeper's per-write
# submission checkpoints per kind (§12.5, M5).
STAGE_ENTRYPOINTS: tuple[str, ...] = (
    "discover", "vet", "scout", "fetch", "plan", "implement", "validate", "review", "package", "watch",
    "catalog", "import",
)  # fmt: skip
SANDBOX_JOB_ENTRYPOINTS: tuple[str, ...] = (
    "fetch", "kiro", "exec", "validate", "package", "unbundle", "snapshot", "list_models", "probe", "extract_text",
)  # fmt: skip
DOCKER_FILTER_OPERATIONS: tuple[str, ...] = ("containers/create", "containers/start")
SUBMISSION_CHECKPOINTS: dict[OutboundKind, tuple[str, ...]] = {
    "open_pr": ("fork", "push", "pull_request"),
    "push_update": ("push",),
    "comment": ("post_comment",),
    "issue_claim": ("post_comment",),
}


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


# ----------------------------------------------------------------------------- control
class ControlView(ApiModel):
    state: ControlStateName
    dry_run: bool
    reason: str | None
    since: str
    config_hash: str
    audit_head: str
    kill_level: Literal["soft", "full"] | None = None
    lifecycle: Literal["up", "draining", "down"] = "up"  # CR-001; kill always overrides it


class ReasonRequest(ApiModel):
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class DrainRequest(ApiModel):
    """CR-001: stop scheduling new work; running work gets ``timeout_s``."""

    reason: Annotated[str, Field(min_length=1, max_length=200)] = "ossctl down"
    timeout_s: Annotated[int, Field(ge=0, le=3600)] = 120
    scope: Literal["distro", "workers"] = "distro"


class DownRequest(ApiModel):
    """CR-001: the drain is over; ``finished``/``interrupted`` feed the Telegram summary."""

    finished: Annotated[int, Field(ge=0)] = 0
    interrupted: Annotated[int, Field(ge=0)] = 0
    scope: Literal["distro", "workers"] = "distro"


class UpRequest(ApiModel):
    resumed: Annotated[int, Field(ge=0)] | None = None  # None: the gatekeeper counts the re-queued stages


class DryRunRequest(ApiModel):
    enabled: bool


class EgressModeRequest(ApiModel):
    """§11.5: ossctl egress mode <m> (TTY-confirmed); Telegram /egress is FEAT-009."""

    mode: Literal["allowlist", "research", "open"]


class EgressModeView(ApiModel):
    mode: Literal["allowlist", "research", "open"]
    default: Literal["allowlist", "research", "open"]
    previous: Literal["allowlist", "research", "open"] | None = None


class HermesTierRequest(ApiModel):
    """§15.5: ossctl hermes tier <tier> (TTY-confirmed); Telegram /hermes tier is FEAT-009."""

    tier: Literal["locked", "research", "self_improve"]


class HermesTierView(ApiModel):
    tier: Literal["locked", "research", "self_improve"]
    default: Literal["locked", "research", "self_improve"]
    toolsets: list[str]
    previous: Literal["locked", "research", "self_improve"] | None = None
    invalidated_skill_approvals: int = 0


class ReloadRequest(ApiModel):
    confirm_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None


# ------------------------------------------------------------------------ contributions
class ContributionCreate(ApiModel):
    repo: Annotated[str, Field(pattern=REPO_RE)]
    issue_number: Annotated[int, Field(ge=1)]
    source: Literal["discover", "hermes_request"]
    score: float | None = None
    # sha256 of the issue's title, body and labels (CR-004 M6: a Skip is re-proposed only on change)
    issue_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None


# ------------------------------------------------------------------- CR-004 proposals
SizeClassName = Literal["xs", "s", "m", "l", "xl", "xxl"]
KIND_RE = r"^[a-z][a-z0-9-]{0,31}$"
Line300 = Annotated[str, Field(min_length=1, max_length=300)]
Line200 = Annotated[str, Field(min_length=1, max_length=200)]


class ProposalEstimate(ApiModel):
    files: Annotated[int, Field(ge=0, le=100_000)]
    code_lines: Annotated[int, Field(ge=0, le=10_000_000)]
    test_lines: Annotated[int, Field(ge=0, le=10_000_000)]
    size_class: SizeClassName = Field(alias="class")


class ProposalAlternative(ApiModel):
    summary: Line300
    est_class: SizeClassName | None = None


class Proposal(ApiModel):
    """Scout output (DESIGN §9.5a). Untrusted model text: validated, canonicalized, rendered escaped."""

    problem: Annotated[str, Field(min_length=1, max_length=2000)]
    approach: Annotated[list[Line300], Field(min_length=1, max_length=10)]
    areas: Annotated[list[Line200], Field(max_length=100)]
    kind: Annotated[str, Field(pattern=KIND_RE)]
    estimate: ProposalEstimate
    tests: Annotated[list[Line300], Field(max_length=50)]
    no_suite_reason: Line300 | None = None
    validation_plan: Annotated[str, Field(max_length=1000)]
    risks: Annotated[list[Line200], Field(max_length=10)] = Field(default_factory=list)
    open_questions: Annotated[list[Line200], Field(max_length=10)] = Field(default_factory=list)
    acceptance_signals: Annotated[list[Line200], Field(max_length=10)] = Field(default_factory=list)
    rationale: Annotated[str, Field(max_length=500)] = ""
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    alternatives: Annotated[list[ProposalAlternative], Field(max_length=10)] = Field(default_factory=list)
    overview: Annotated[str, Field(max_length=8000)] = ""  # scout's codebase overview (Details)
    citations: Annotated[list[Annotated[str, Field(max_length=500)]], Field(max_length=30)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def _tests_or_reason(self) -> Proposal:
        """Tests are expected (§9.14): name them, or say why the repo has no suite."""
        if not self.tests and not self.no_suite_reason:
            raise ValueError("tests is empty: name the tests to add, or give no_suite_reason")
        return self


class ProposalSubmit(ApiModel):
    """``POST /v1/contributions/{id}/proposal`` (worker): a new version (1, 2, ...)."""

    proposal: Proposal
    revises: Annotated[int, Field(ge=1)] | None = None  # the version this one answers (adjust/smaller)
    feedback: Annotated[str, Field(max_length=2000)] | None = None  # "You asked" (echoed from the request)


WORK_URL_RE = (
    r"^(?:https://)?(?:www\.)?github\.com/(?P<owner>[A-Za-z0-9-]{1,39})/(?P<repo>[A-Za-z0-9._-]{1,100})"
    r"(?:/(?P<kind>issues|pull)/(?P<n>[1-9][0-9]{0,9}))?/?$"
)


class DiscoverRequest(ApiModel):
    """``POST /v1/discover`` (operator, ``ossctl discover``): [] = every enabled target."""

    repos: Annotated[list[Annotated[str, Field(min_length=3, max_length=140)]], Field(max_length=50)] = []


class WorkRequest(ApiModel):
    """``POST /v1/work`` (operator; Telegram /work in item 47): user-directed work (👤)."""

    url: Annotated[str, Field(min_length=1, max_length=400)]
    notes: Annotated[str, Field(max_length=1000)] = ""


class PrefsUpdate(ApiModel):
    """``PUT /v1/discovery-prefs``: size and filter preferences (never the ceiling)."""

    scope: Annotated[str, Field(pattern=r"^(\*|" + REPO_RE.strip("^$") + r")$")] = "*"
    autonomous_max_class: Literal["xs", "s", "m", "l", "xl", "inherit"] | None = None
    preferred_classes: list[Literal["xs", "s", "m", "l", "xl"]] | None = None
    filters: dict[str, Any] | None = None  # keys checked against discovery_prefs.FILTER_KEYS


class StageRunOpen(ApiModel):
    """``POST /v1/contributions/{id}/stage-runs`` (worker, §9.3): open a stage run before the stage."""

    stage: Annotated[str, Field(pattern=r"^[a-z_]{1,32}$")]
    profile: Annotated[str, Field(pattern=r"^[a-z-]{1,32}$")] | None = None
    coder_mode: Literal["split"] | None = None
    model: Annotated[str, Field(pattern=MODEL_ID_RE)] | None = None
    effort: Effort | None = None


class StageRunUpdate(ApiModel):
    """``POST /v1/stage-runs/{id}``: heartbeat (no fields) or close with a final status."""

    status: Literal["running", "succeeded", "failed", "interrupted", "cancelled"] | None = None
    model_actual: Annotated[str, Field(max_length=128)] | None = None
    exit_code: int | None = None
    error_class: Annotated[str, Field(pattern=r"^[a-z0-9_:.-]{1,64}$")] | None = None
    error_message: Annotated[str, Field(max_length=500)] | None = None
    log_path: Annotated[str, Field(max_length=300)] | None = None


class PrefsUndo(ApiModel):
    undo_id: Annotated[str, Field(pattern=r"^u_[0-9A-Z]{10}$")]


class TransitionRequest(ApiModel):
    # Enum fields are lax: strict mode would only accept enum instances from a parsed JSON dict.
    from_: State = Field(alias="from", strict=False)
    to: State = Field(strict=False)
    evidence: dict[str, Any] = Field(default_factory=dict)


class TransitionResponse(ApiModel):
    state: State
    redirected_from: State | None = None


class PackageTsRequest(ApiModel):
    rev: Annotated[int, Field(ge=1, le=10_000)]


SHA1_RE = r"^[0-9a-f]{40}$"
SHA256_RE = r"^[0-9a-f]{64}$"
BASE_REF_RE = r"^[A-Za-z0-9._/-]{1,100}$"
PACKAGE_ARTIFACTS: tuple[str, ...] = ("change.bundle", "change.patch", "diffstat.json")
PackageArtifactName = Literal["change.bundle", "change.patch", "diffstat.json"]


class PackageRequest(ApiModel):
    """``POST /v1/contributions/{id}/package`` (§12.2, §12.5). The kind is the gatekeeper's choice."""

    rev: Annotated[int, Field(ge=1, le=10_000)]
    artifact_dir: Annotated[str, Field(min_length=1, max_length=300)]
    sha256s: dict[PackageArtifactName, Annotated[str, Field(pattern=SHA256_RE)]]
    pr_title: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    pr_body: Annotated[str, Field(max_length=60000)] | None = None
    claimed_head_sha: Annotated[str, Field(pattern=SHA1_RE)]
    base_ref: Annotated[str, Field(pattern=BASE_REF_RE)]
    base_sha: Annotated[str, Field(pattern=SHA1_RE)]
    validation: dict[str, Any] = Field(default_factory=dict)
    review: dict[str, Any] = Field(default_factory=dict)

    @field_validator("sha256s")
    @classmethod
    def _all_artifacts(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) != set(PACKAGE_ARTIFACTS):
            raise ValueError(f"sha256s must name exactly {list(PACKAGE_ARTIFACTS)}")
        return value


class ImportRequest(ApiModel):
    """Operator import (M3): bundle bytes, never a path (decoded <= 50 MB, body <= 70 MB)."""

    repo: Annotated[str, Field(pattern=REPO_RE)]
    base_ref: Annotated[str, Field(pattern=BASE_REF_RE)]
    base_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    bundle_b64: Annotated[str, Field(min_length=4, max_length=70 * 1024 * 1024)]
    bundle_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    title: Annotated[str, Field(min_length=1, max_length=256)]
    body: Annotated[str, Field(max_length=60000)]
    issue_number: Annotated[int, Field(ge=1)] | None = None


# ----------------------------------------------------------------------------- actions
class ActionCreate(ApiModel):
    cid: Annotated[str, Field(pattern=r"^c_[0-9A-Z]{10}$")]
    kind: Literal["issue_claim", "comment"]  # push_update/open_pr only via /package (M9)
    target_number: Annotated[int, Field(ge=1)]
    body: Annotated[str, Field(min_length=1, max_length=20000)]


class ProbeResult(ApiModel):
    probe_key: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    probe: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,64}$")]
    passed: bool
    evidence_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    # FEAT-006 (owner decision B): a model refusal is "inconclusive", never a pass and never a kill
    verdict: Literal["pass", "fail", "inconclusive"] | None = None
    attempts: Annotated[int, Field(ge=1, le=10)] = 1


# ------------------------------------------------------------------------------ models
class CatalogEntry(ApiModel):
    model_id: Annotated[str, Field(pattern=MODEL_ID_RE)]
    name: Annotated[str, Field(max_length=200)] | None = None
    # kiro-cli ModelInfo.supported_input_types (e.g. TEXT, IMAGE); the vision role needs IMAGE
    input_types: Annotated[list[Annotated[str, Field(pattern=r"^[A-Za-z_]{1,32}$")]], Field(max_length=16)] | None = (
        None
    )


class CatalogUpsert(ApiModel):
    models: Annotated[list[CatalogEntry], Field(min_length=1, max_length=200)]

    @field_validator("models")
    @classmethod
    def _unique(cls, value: list[CatalogEntry]) -> list[CatalogEntry]:
        ids = [m.model_id for m in value]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate model_id")
        return value


class ModelSetRequest(ApiModel):
    model: Annotated[str, Field(pattern=MODEL_ID_RE)]
    effort: Effort | None = None
    force: bool = False


# ------------------------------------------------------------------------ misc requests
DISCOVERY_URL_RE = r"^https://github\.com/[\w.-]+/[\w.-]+/issues/\d+$"


class DiscoveryRequest(ApiModel):
    url: Annotated[str, Field(pattern=DISCOVERY_URL_RE, max_length=300)]


class NotifyRequest(ApiModel):
    text: Annotated[str, Field(min_length=1, max_length=4000)]
