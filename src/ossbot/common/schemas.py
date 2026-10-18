"""Shared enums, constants and gatekeeper API models (``extra="forbid"`` everywhere).

The stage/entrypoint constants are the single source shared by the runner, sandboxd, the
Docker filter and ``tests/unit/gatekeeper/test_control.py`` (AC 10), so later components extend
the same kill/pause parametrization.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


TERMINAL_STATES: frozenset[State] = frozenset(
    {
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
ContributionSource = Literal["discover", "hermes_request", "operator"]
ControlStateName = Literal["running", "paused", "killed"]

# Runner stage entrypoints (§9.1), sandboxd job entrypoints (sandbox/entrypoints/*.sh), the
# Docker filter operations refused while paused/killed (N5), and the gatekeeper's per-write
# submission checkpoints per kind (§12.5, M5).
STAGE_ENTRYPOINTS: tuple[str, ...] = (
    "discover", "vet", "fetch", "plan", "implement", "validate", "review", "package", "watch",
    "catalog", "import",
)  # fmt: skip
SANDBOX_JOB_ENTRYPOINTS: tuple[str, ...] = (
    "fetch", "kiro", "exec", "validate", "package", "unbundle", "snapshot", "list_models", "probe",
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
    resumed: Annotated[int, Field(ge=0)] = 0


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


# ------------------------------------------------------------------------------ models
class CatalogEntry(ApiModel):
    model_id: Annotated[str, Field(pattern=MODEL_ID_RE)]
    name: Annotated[str, Field(max_length=200)] | None = None


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
