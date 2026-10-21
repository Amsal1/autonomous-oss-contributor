"""Config schemas (pydantic v2, ``extra="forbid"``, strict) and loaders.

Security-relevant files (policy, targets, egress, profile) reach components only through the
gatekeeper's config snapshot (DESIGN §7.9). ``models.yaml`` is the only live-read file (§16).
Flags whose non-v1 values are phase-2 features accept only the v1 value (PLAN "Deferred").
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ossbot.common.control_flags import EGRESS_MODES, EgressMode, HermesTier
from ossbot.common.size import CLASS_BOUNDS, SelectableSizeClass
from ossbot.common.yamlio import YamlError, safe_load

# telegram.yaml (DESIGN §14.20.3) is optional in a snapshot: a snapshot written before it existed, or a
# checkout without it, gets the schema defaults (REQUIRED_SNAPSHOT_FILES must all be present).
SNAPSHOT_FILES: tuple[str, ...] = ("egress.yaml", "policy.yaml", "profile.yaml", "targets.yaml", "telegram.yaml")
REQUIRED_SNAPSHOT_FILES: tuple[str, ...] = ("egress.yaml", "policy.yaml", "profile.yaml", "targets.yaml")
REPO_RE = r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$"
MODEL_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
HOST_RE = r"^\.?[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*$"
GITHUB_LOGIN_RE = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$"

NonNegInt = Annotated[int, Field(ge=0)]
PosInt = Annotated[int, Field(ge=1)]


class ConfigError(ValueError):
    """A config file failed to parse or validate. ``errors`` maps file name -> messages."""

    def __init__(self, errors: Mapping[str, list[str]]) -> None:
        self.errors = {k: list(v) for k, v in errors.items()}
        super().__init__("; ".join(f"{k}: {' | '.join(v)}" for k, v in self.errors.items()))


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


# ------------------------------------------------------------------------------------ policy
class SandboxPolicy(Strict):
    coder_mode: Literal["split", "single"] = "split"
    coder_single_mode_evidence: str | None = None
    # §15.3/§10.9: "all" runs kiro-cli with --trust-all-tools and needs probes passed under "all".
    kiro_trust_mode: Literal["ids", "all"] = "ids"

    @model_validator(mode="after")
    def _single_is_phase2(self) -> SandboxPolicy:
        if self.coder_mode == "single":
            raise ValueError("sandbox.coder_mode 'single' is a phase-2 feature and is rejected in v1")
        return self


class Limits(Strict):
    prs_per_24h: NonNegInt = 2
    open_prs_per_repo: NonNegInt = 1
    comments_per_24h: NonNegInt = 10
    issue_claims_per_24h: NonNegInt = 2
    max_concurrent_pipelines: PosInt = 2
    max_awaiting_approval: PosInt = 5
    approval_ttl_hours: PosInt = 72
    confirm_ttl_seconds: PosInt = 300
    contribution_expiry_days: PosInt = 7
    watch_rounds_max: NonNegInt = 3
    repo_cooldown_days_after_close: NonNegInt = 30
    repo_cooldown_days_after_user_reject: NonNegInt = 7
    rediscover_cooldown_days: NonNegInt = 7
    max_implement_attempts: NonNegInt = 2
    max_review_rounds: NonNegInt = 2
    max_change_rounds: NonNegInt = 5


class DiffPolicy(Strict):
    """The hard ceiling (CR-004 Amendment B): a runaway-agent guard, changed only in policy.yaml.

    ``max_changed_lines`` counts non-test lines and ``max_test_lines`` test lines (split by
    ``size.test_globs``, ``size.exclude_globs`` dropped from both); every path counts toward
    ``max_files`` (decision B5)."""

    max_files: PosInt = 80
    max_changed_lines: PosInt = 3000
    max_test_lines: PosInt = 3000
    forbidden_paths: list[str] = Field(default_factory=list)
    forbid_binary: bool = True
    forbid_submodule_changes: bool = True
    forbid_symlinks: bool = True
    forbid_file_modes: list[Annotated[str, Field(pattern=r"^[0-7]{6}$")]] = Field(
        default_factory=lambda: ["120000", "160000"]
    )


class TextPolicy(Strict):
    title_max: PosInt = 256
    body_max: Annotated[int, Field(ge=1, le=65536)] = 60000
    comment_max: PosInt = 20000
    commit_message_max: PosInt = 10000
    commit_subject_max: PosInt = 100
    confidentiality_denylist: list[str] = Field(default_factory=list)
    require_disclosure: bool = True


class SecretsPolicy(Strict):
    gitleaks: bool = True
    extra_patterns: list[str] = Field(default_factory=list)
    kiro_key_hash_check: bool = True

    @field_validator("extra_patterns")
    @classmethod
    def _compiles(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
        return value


class AnomalyPolicy(Strict):
    max_consecutive_stage_failures: PosInt = 5
    max_proxy_denies_per_task: PosInt = 50
    max_kiro_invocations_per_hour: PosInt = 120


class StackValidation(Strict):
    enabled: Literal[False] = False  # phase 2 (§10.7)


class ValidationPolicy(Strict):
    stack: StackValidation = StackValidation()


DEFAULT_EXCLUDE_GLOBS: tuple[str, ...] = (
    "**/*.lock", "**/package-lock.json", "**/go.sum", "**/__snapshots__/**", "**/vendor/**",
    "**/*.min.js", "**/*_generated.*", "**/*.pb.go",
)  # fmt: skip
DEFAULT_TEST_GLOBS: tuple[str, ...] = (
    "**/test/**", "**/tests/**", "**/*_test.*", "**/test_*.py", "**/*.test.*", "**/*.spec.*", "**/__tests__/**",
)  # fmt: skip
DEFAULT_PREFERRED_CLASSES: tuple[SelectableSizeClass, ...] = ("xs", "s")


class SizePolicy(Strict):
    """Soft size preference for autonomous discovery (DESIGN §9.14); never a rule against the user."""

    autonomous_max_class: SelectableSizeClass = "s"
    preferred_classes: list[SelectableSizeClass] = Field(default_factory=lambda: list(DEFAULT_PREFERRED_CLASSES))
    overrun_tolerance: Annotated[float, Field(ge=0.0, le=10.0)] = 0.5
    on_overrun: Literal["flag", "reapprove"] = "flag"
    exclude_globs: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDE_GLOBS))
    test_globs: list[str] = Field(default_factory=lambda: list(DEFAULT_TEST_GLOBS))


class DiscoveryFilters(Strict):
    """Autonomous discovery only (DESIGN §9.14); user-directed work skips them."""

    allowed_kinds: list[str] = Field(
        default_factory=lambda: ["bug", "docs", "tests", "small-feature", "ci-fix", "refactor-small"]
    )
    excluded_kinds: list[str] = Field(
        default_factory=lambda: ["breaking-change", "new-dependency", "api-redesign", "security-sensitive-crypto"]
    )
    require_labels_any: list[str] = Field(default_factory=list)
    max_issue_age_days: PosInt = 180
    min_maintainer_activity_days: PosInt = 60
    languages: list[str] = Field(default_factory=list)
    validation_tier_max: Literal["T0", "T1", "T2"] = "T1"
    # CR-004 §2 / §14.14.4 page 4: autonomous discovery skips a repo with this many of our PRs open.
    # The submit-time live check (policy.limits.open_prs_per_repo) stays the hard limit for every PR.
    max_open_prs_by_me_per_repo: Annotated[int, Field(ge=1, le=3)] = 1


class ProposalPolicy(Strict):
    """Gate 1 (DESIGN §9.5b, §9.14)."""

    ttl_hours: PosInt = 72
    max_per_day: PosInt = 5
    claim_comment: Literal["off", "on"] = "off"
    max_concurrent_scouts: PosInt = 2


class DiscoveryPolicy(Strict):
    proactive: Literal[False] = False  # phase 2
    interval_hours: PosInt = 6
    top_n: PosInt = 5
    proposals_per_cycle: PosInt = 3  # §9.14: proposal cards per discovery cycle


class TelegramPolicy(Strict):
    mode: Literal["single"] = "single"  # 'dual' is phase 2


class HermesPolicy(Strict):
    """§15.5: the default tier; the active one is the ``hermes-tier`` control flag (fail closed ``locked``)."""

    capability_tier_default: HermesTier = "self_improve"
    # Rendered as Hermes' ``model.context_length``: Hermes cannot look up the window of the provider
    # model "kiro-acp" (the real model is chosen per turn from models.yaml), so it is set here.
    context_length: int = Field(default=200_000, ge=8192, le=2_000_000)


AttachmentKind = Literal["image", "pdf", "text", "audio"]
ATTACHMENT_KINDS: tuple[AttachmentKind, ...] = ("image", "pdf", "text", "audio")


class AttachmentsPolicy(Strict):
    """§15.6: files the owner sends the assistant (downloaded by the gatekeeper through proxy 3150)."""

    max_mb: Annotated[int, Field(ge=1, le=20)] = 20  # Bot API getFile serves at most 20 MB
    strip_metadata: bool = True
    allowed: list[AttachmentKind] = Field(default_factory=lambda: list(ATTACHMENT_KINDS))
    pdf_text_max_bytes: Annotated[int, Field(ge=1024, le=2 * 1024 * 1024)] = 2 * 1024 * 1024
    retention_hours: Annotated[int, Field(ge=1, le=720)] = 24


class ChatPolicy(Strict):
    attachments: AttachmentsPolicy = AttachmentsPolicy()


class BackupPolicy(Strict):
    include_secrets: bool = False
    keep: PosInt = 8


class RetentionPolicy(Strict):
    """Time-based retention (DESIGN §18.10); no size caps. Journal retention is the journald drop-in.

    The gatekeeper deletes its own DB rows and repos (``gatekeeper/retention.py``); root deletes the
    root-reachable stores (``ossctl gc``)."""

    # terminal contributions' mirrors, derived, imports, work bundles, artifacts; unused cache repos
    contributions_days: Annotated[int, Field(ge=7, le=3650)] = 30
    telegram_rows_days: Annotated[int, Field(ge=30, le=3650)] = 90  # closed tg_outbox, tg_views, ui_tokens
    run_logs_days: Annotated[int, Field(ge=7, le=3650)] = 30  # /srv/ossbot/logs/runs/*.log
    hermes_sessions_days: Annotated[int, Field(ge=7, le=3650)] = 90  # /home/hermes/.hermes/sessions/* (idle)
    tmp_days: Annotated[int, Field(ge=1, le=365)] = 7  # /tmp top-level leftovers
    docker_build_cache_hours: Annotated[int, Field(ge=24, le=8760)] = 168  # unused Docker build cache
    disk_warn_gib: Annotated[int, Field(ge=5, le=1024)] = 40  # doctor WARN when / uses more


class LifecyclePolicy(Strict):
    """CR-001 graceful down: running work gets ``drain_timeout_s``; ``up`` waits for health.

    ``drain_timeout_s`` is capped at 120 (= ``control_flags.DEFAULT_DRAIN_TIMEOUT_S``): the units'
    ``TimeoutStopSec=150``, sandboxd's SIGTERM drain (``OSSBOT_DRAIN_TIMEOUT_S=120``) and the
    ``down.ps1`` cut-off (120 + 90 s without ``-Timeout``) are static and sized for it (Phase 1
    review P1-9). A longer drain is a per-run choice: ``ossctl down --timeout S`` / ``down.ps1
    -Timeout S``."""

    drain_timeout_s: Annotated[int, Field(ge=10, le=120)] = 120
    up_health_timeout_s: Annotated[int, Field(ge=10, le=900)] = 180


class PolicyConfig(Strict):
    version: Literal[1]
    dry_run_default: Literal[True] = True  # I7: dry-run is ON at first start and after any reset
    lifecycle: LifecyclePolicy = LifecyclePolicy()
    sandbox: SandboxPolicy = SandboxPolicy()
    limits: Limits = Limits()
    diff: DiffPolicy = DiffPolicy()
    text: TextPolicy = TextPolicy()
    secrets: SecretsPolicy = SecretsPolicy()
    anomaly: AnomalyPolicy = AnomalyPolicy()
    validation: ValidationPolicy = ValidationPolicy()
    size: SizePolicy = SizePolicy()
    discovery_filters: DiscoveryFilters = DiscoveryFilters()
    proposal: ProposalPolicy = ProposalPolicy()
    discovery: DiscoveryPolicy = DiscoveryPolicy()
    telegram: TelegramPolicy = TelegramPolicy()
    hermes: HermesPolicy = HermesPolicy()
    chat: ChatPolicy = ChatPolicy()
    backup: BackupPolicy = BackupPolicy()
    retention: RetentionPolicy = RetentionPolicy()

    @model_validator(mode="after")
    def _size_class_within_ceiling(self) -> PolicyConfig:
        max_lines, max_files = CLASS_BOUNDS[self.size.autonomous_max_class]
        if max_lines > self.diff.max_changed_lines or max_files > self.diff.max_files:
            raise ValueError(
                f"size.autonomous_max_class {self.size.autonomous_max_class!r} ({max_lines} lines, {max_files} files)"
                f" exceeds the hard ceiling diff ({self.diff.max_changed_lines} lines, {self.diff.max_files} files)"
            )
        return self


# ----------------------------------------------------------------------------------- targets
ValidationKind = Literal["static", "unit", "smoke"]
DEFAULT_VALIDATION: tuple[ValidationKind, ...] = ("static", "unit", "smoke")


class TargetDefaults(Strict):
    enabled: bool = True
    labels: list[str] = Field(default_factory=list)
    skip_labels: list[str] = Field(default_factory=list)
    clone_depth: PosInt = 200
    # Autonomous-discovery preference only (§17.1, CR4-2o); None = size.autonomous_max_class.
    # Never a hard limit: the only cap is the policy.diff ceiling.
    size_class: SelectableSizeClass | None = None
    validation: list[ValidationKind] = Field(default_factory=lambda: list(DEFAULT_VALIDATION))
    stack_validation: Literal["none"] = "none"  # compose | kind are phase 2
    cla_signed: bool = False
    ai_policy_override: Literal["allowed"] | None = None
    egress_extra: list[Annotated[str, Field(pattern=HOST_RE)]] = Field(default_factory=list)


class RepoEntry(Strict):
    repo: Annotated[str, Field(pattern=REPO_RE)]
    enabled: bool | None = None
    discovery: bool = True
    relation: str | None = None
    note: str | None = None
    extra_labels: list[str] = Field(default_factory=list)
    clone_depth: PosInt | None = None
    size_class: SelectableSizeClass | Literal["inherit"] | None = None  # autonomous preference only
    validation: list[ValidationKind] | None = None
    cla_signed: bool | None = None
    ai_policy_override: Literal["allowed"] | None = None
    egress_extra: list[Annotated[str, Field(pattern=HOST_RE)]] = Field(default_factory=list)
    disclosure_template: str | None = None


class Category(Strict):
    id: Annotated[str, Field(pattern=r"^[a-z0-9-]{1,40}$")]
    priority: Annotated[int, Field(ge=1, le=5)]
    why: str
    repos: list[RepoEntry]


class DenyEntry(Strict):
    pattern: str


class TargetsConfig(Strict):
    version: Literal[1]
    defaults: TargetDefaults = TargetDefaults()
    categories: list[Category]
    denylist: list[DenyEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_and_not_denied(self) -> TargetsConfig:
        seen: set[str] = set()
        for category in self.categories:
            for entry in category.repos:
                key = entry.repo.lower()
                if key in seen:
                    raise ValueError(f"duplicate repo {entry.repo}")
                seen.add(key)
                if self.is_denied(entry.repo):
                    raise ValueError(f"repo {entry.repo} matches the denylist")
        return self

    def is_denied(self, repo: str) -> bool:
        return any(fnmatch.fnmatchcase(repo.lower(), d.pattern.lower()) for d in self.denylist)


@dataclass(frozen=True)
class ResolvedTarget:
    repo: str
    category: str
    priority: int
    enabled: bool
    discovery: bool
    labels: tuple[str, ...]
    skip_labels: tuple[str, ...]
    clone_depth: int
    size_class: str | None  # autonomous preference; None = inherit size.autonomous_max_class
    validation: tuple[str, ...]
    cla_signed: bool
    ai_policy_override: str | None
    egress_extra: tuple[str, ...]
    disclosure_template: str | None


def all_targets(targets: TargetsConfig) -> list[ResolvedTarget]:
    """Every configured repo with defaults merged (discovery order: category priority, file order)."""
    out = [
        resolve_target(targets, e.repo) for c in sorted(targets.categories, key=lambda c: c.priority) for e in c.repos
    ]
    return [t for t in out if t is not None]


def resolve_target(targets: TargetsConfig, repo: str) -> ResolvedTarget | None:
    """Return the effective settings for ``repo`` (defaults merged), or None if not a target."""
    if targets.is_denied(repo):
        return None
    d = targets.defaults
    for category in targets.categories:
        for e in category.repos:
            if e.repo.lower() != repo.lower():
                continue
            return ResolvedTarget(
                repo=e.repo,
                category=category.id,
                priority=category.priority,
                enabled=d.enabled if e.enabled is None else e.enabled,
                discovery=e.discovery,
                labels=tuple(dict.fromkeys([*d.labels, *e.extra_labels])),
                skip_labels=tuple(d.skip_labels),
                clone_depth=e.clone_depth or d.clone_depth,
                size_class=None if e.size_class == "inherit" else (e.size_class or d.size_class),
                validation=tuple(e.validation or d.validation),
                cla_signed=d.cla_signed if e.cla_signed is None else e.cla_signed,
                ai_policy_override=e.ai_policy_override or d.ai_policy_override,
                egress_extra=(*d.egress_extra, *e.egress_extra),
                disclosure_template=e.disclosure_template,
            )
    return None


# ------------------------------------------------------------------------------------ egress
SandboxProfileName = Literal["fetch", "validate", "research", "agent-llm", "catalog"]
HostCallerName = Literal["runner", "hermes", "gatekeeper"]


class HostCaller(Strict):
    port: Annotated[int, Field(ge=1024, le=65535)]
    categories: list[str]


class AlwaysDenied(Strict):
    cidrs: list[str]
    domains: list[Annotated[str, Field(pattern=HOST_RE)]]


# Egress modes (§11.5, decision B3): a mode only ADDS categories, and only to this closed set of
# grantees. fetch, validate, runner and gatekeeper are not keys, so data can never widen what
# untrusted code (or the trusted writers) can reach; the renderer asserts the invariants on top.
ModeGrantee = Literal["research", "hermes", "agent-llm", "catalog"]
TELEGRAPH_CATEGORY = "telegraph"  # DESIGN §14.12.8: the gatekeeper's opt-in Telegraph egress


class EgressConfig(Strict):
    version: Literal[1]
    categories: dict[str, list[Annotated[str, Field(pattern=HOST_RE)]]]
    denied_by_default: list[str] = Field(default_factory=list)
    sandbox_profiles: dict[SandboxProfileName, list[str]]
    host_callers: dict[HostCallerName, HostCaller]
    always_denied: AlwaysDenied
    mode_default: EgressMode = "open"
    modes: dict[EgressMode, dict[ModeGrantee, list[str]]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _references_exist(self) -> EgressConfig:
        missing_modes = [m for m in EGRESS_MODES if m not in self.modes]
        if missing_modes:
            raise ValueError(f"modes must define every egress mode; missing {missing_modes}")
        known = set(self.categories) | {"web_public", "kiro_idc_login", "pkg_helm"}
        refs = [*self.denied_by_default]
        for cats in self.sandbox_profiles.values():
            refs.extend(cats)
        for caller in self.host_callers.values():
            refs.extend(caller.categories)
        for grants in self.modes.values():
            for cats in grants.values():
                refs.extend(cats)
        unknown = sorted(set(refs) - known)
        if unknown:
            raise ValueError(f"unknown egress categories: {unknown}")
        if TELEGRAPH_CATEGORY in refs:  # §14.12.8: rendered only for the gatekeeper, only while enabled
            raise ValueError("the telegraph category can't be listed by a profile, caller or mode")
        ports = [c.port for c in self.host_callers.values()]
        if len(ports) != len(set(ports)):
            raise ValueError("host caller ports must be distinct")
        return self


# ----------------------------------------------------------------------------------- profile
QUIET_HOURS_RE = r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$"


class TelegramProfile(Strict):
    user_id: NonNegInt = 0  # 0 = not configured; the Telegram front-end stays disabled
    # DESIGN §14.13.2, §14.20.6: the owner's IANA zone (quiet hours, digests) and quiet hours HH:MM-HH:MM
    # (may wrap midnight); unset = UTC and no quiet hours.
    timezone: Annotated[str, Field(min_length=1, max_length=64)] = "UTC"
    quiet_hours: Annotated[str, Field(pattern=QUIET_HOURS_RE)] | None = None

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA time zone {value!r}") from exc
        return value


class KiroProfile(Strict):
    idc_start_url: Annotated[str, Field(pattern=r"^https://[A-Za-z0-9.-]+(/[^\s]*)?$")]
    idc_region: Annotated[str, Field(pattern=r"^[a-z]{2}-[a-z]+-\d$")]


class BackupProfile(Strict):
    age_recipients: list[str] = Field(default_factory=list)


class InterestsProfile(Strict):
    summary: str = ""
    categories_priority: dict[str, int] = Field(default_factory=dict)
    prior_contributions: list[Annotated[str, Field(pattern=REPO_RE)]] = Field(default_factory=list)


class ReviewMirrorProfile(Strict):
    """ADR-0019 option C: the owner's private mirror repo (e.g. ``owner/ossbot-review``); unset = no mirror."""

    repo: Annotated[str, Field(pattern=REPO_RE)] | None = None


class ProfileConfig(Strict):
    version: Literal[1]
    github_login: Annotated[str, Field(pattern=GITHUB_LOGIN_RE)]
    commit_name: Annotated[str, Field(min_length=1, max_length=200)]
    commit_email: Annotated[str, Field(pattern=r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")]
    signoff: Literal[True] = True
    forbidden_logins: list[Annotated[str, Field(pattern=GITHUB_LOGIN_RE)]] = Field(default_factory=list)
    confidentiality_floor: list[Annotated[str, Field(min_length=3)]] = Field(default_factory=list)
    telegram: TelegramProfile = TelegramProfile()
    kiro: KiroProfile
    backup: BackupProfile = BackupProfile()
    interests: InterestsProfile = InterestsProfile()
    review_mirror: ReviewMirrorProfile = ReviewMirrorProfile()

    @model_validator(mode="after")
    def _login_not_forbidden(self) -> ProfileConfig:
        if self.github_login.lower() in {x.lower() for x in self.forbidden_logins}:
            raise ValueError("github_login is listed in forbidden_logins")
        return self

    @property
    def identity(self) -> str:
        return f"{self.commit_name} <{self.commit_email}>"


# ------------------------------------------------------------------------------------ models
Effort = Literal["low", "medium", "high", "xhigh", "max"]
# scout (CR-004) and vision (CR-002 Amendment A) are revision-5 roles (DESIGN §16.1); test and
# test_vision (owner decision 2026-10-18) are the models of every live check that calls a model.
RoleName = Literal["chat", "planner", "vetter", "coder", "reviewer", "summarizer", "scout", "vision", "test",
                   "test_vision"]  # fmt: skip
ROLE_NAMES: tuple[str, ...] = ("chat", "planner", "vetter", "coder", "reviewer", "summarizer", "scout", "vision",
                               "test", "test_vision")  # fmt: skip
ModelRef = Annotated[str, Field(pattern=MODEL_ID_RE)]  # 'default', a role name (inherit) or a catalog id


class DefaultModel(Strict):
    model: ModelRef
    effort: Effort = "high"


class RoleModel(Strict):
    model: ModelRef  # 'default', another role's name (inherit that role) or a catalog id
    effort: Effort | None = None
    require_distinct_from: RoleName | None = None
    # "none": never fall back (fail with a clear message); a list: these ids in order; unset: the
    # role it inherits from decides, else the global ``fallbacks``
    fallback: Literal["none"] | list[ModelRef] | None = None


class RolesConfig(Strict):
    chat: RoleModel
    planner: RoleModel
    vetter: RoleModel
    coder: RoleModel
    reviewer: RoleModel
    summarizer: RoleModel
    scout: RoleModel
    vision: RoleModel
    test: RoleModel
    test_vision: RoleModel


class CatalogConfig(Strict):
    refresh_hours: PosInt = 24
    notify_new_models: bool = True


# Pipeline model backends (ADR-0022, ``common.backends``): only registered names validate.
PipelineBackendName = Literal["kiro"]


class ModelsConfig(Strict):
    version: Literal[1]
    backend: PipelineBackendName = "kiro"  # omitted = kiro (today's behaviour)
    default: DefaultModel
    roles: RolesConfig
    fallbacks: list[ModelRef] = Field(default_factory=list)
    catalog: CatalogConfig = CatalogConfig()

    @model_validator(mode="after")
    def _references(self) -> ModelsConfig:
        """``default`` names a model id; a role may name ``default`` or another role (no cycles)."""
        refs = {"default", *ROLE_NAMES}
        if self.default.model in refs:
            raise ValueError(f"default.model must be a model id, not {self.default.model!r}")
        for ref in self.fallbacks:
            if ref in refs:
                raise ValueError(f"fallbacks must be model ids, not {ref!r}")
        for name in ROLE_NAMES:
            entry: RoleModel = getattr(self.roles, name)
            if isinstance(entry.fallback, list) and any(ref in refs for ref in entry.fallback):
                raise ValueError(f"roles.{name}.fallback must list model ids")
            if entry.require_distinct_from == name:
                raise ValueError(f"roles.{name}.require_distinct_from names the role itself")
            seen = [name]
            while entry.model in ROLE_NAMES:
                if entry.model in seen:
                    raise ValueError(f"roles.{name}: role reference cycle {' -> '.join([*seen, entry.model])}")
                seen.append(entry.model)
                entry = getattr(self.roles, entry.model)
        return self


# ---------------------------------------------------------------------------------- telegram
HHMM_RE = r"^([01]\d|2[0-3]):[0-5]\d$"
WEEKLY_RE = r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) ([01]\d|2[0-3]):[0-5]\d$"


class TgInterval(Strict):
    min_edit_interval_s: Annotated[int, Field(ge=1, le=3600)]


class DashboardConfig(TgInterval):
    min_edit_interval_s: Annotated[int, Field(ge=30, le=3600)] = 30  # §14.17.4: dashboard <= 1 edit / 30 s


class StatusCardConfig(TgInterval):
    min_edit_interval_s: Annotated[int, Field(ge=60, le=3600)] = 60


class ProgressConfig(TgInterval):
    min_edit_interval_s: Annotated[int, Field(ge=5, le=3600)] = 5


class DraftsConfig(Strict):
    min_interval_s: Annotated[float, Field(ge=1.0, le=10.0)] = 1.0
    heartbeat_s: Annotated[int, Field(ge=5, le=20)] = 20  # a draft at least every 20 s (§14.12.1)


class DigestConfig(Strict):
    daily: Annotated[str, Field(pattern=HHMM_RE)] = "21:30"
    weekly: Annotated[str, Field(pattern=WEEKLY_RE)] = "Sun 18:00"


class EffectsConfig(Strict):
    merged: Annotated[str, Field(pattern=r"^[0-9]{1,32}$")] | None = None  # message_effect_id (§14.23 item 5)


class TgAttachmentsConfig(Strict):
    max_mb: Annotated[int, Field(ge=1, le=20)] = 20
    album_window_s: Annotated[float, Field(ge=0.5, le=5.0)] = 1.5


class ReviewMirrorConfig(Strict):
    enabled: bool = False  # ADR-0019 C; takes effect only after `ossctl review-mirror enable` (U9)
    poll_pending_s: Annotated[int, Field(ge=30, le=3600)] = 60
    poll_other_s: Annotated[int, Field(ge=60, le=86400)] = 600


class TelegraphConfig(Strict):
    enabled: bool = False  # §14.12.8; never automatic
    offer_min_chars: Annotated[int, Field(ge=200, le=60000)] = 1500
    max_content_kb: Annotated[int, Field(ge=1, le=60)] = 60
    author_name: Annotated[str, Field(max_length=128)] = ""


class FeaturesConfig(Strict):
    enabled: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]] = Field(default_factory=list)


class TelegramConfig(Strict):
    """``config/telegram.yaml`` (DESIGN §14.20.3): presentation only; nothing here can approve,
    publish or raise a limit. Opt-in surfaces (mirror, Telegraph) stay off until a confirmed
    ``/reload`` plus their own enable step (U9)."""

    version: Literal[1] = 1
    topics: Literal["auto", "off"] = "auto"
    rich_views_default: Literal["auto", "on", "off"] = "auto"
    loud_cards_per_hour: Annotated[int, Field(ge=0, le=20)] = 2
    dashboard: DashboardConfig = DashboardConfig()
    status_card: StatusCardConfig = StatusCardConfig()
    progress: ProgressConfig = ProgressConfig()
    drafts: DraftsConfig = DraftsConfig()
    stale_message_minutes: Annotated[int, Field(ge=1, le=1440)] = 10
    digest: DigestConfig = DigestConfig()
    effects: EffectsConfig = EffectsConfig()
    attachments: TgAttachmentsConfig = TgAttachmentsConfig()
    review_mirror: ReviewMirrorConfig = ReviewMirrorConfig()
    telegraph: TelegraphConfig = TelegraphConfig()
    features: FeaturesConfig = FeaturesConfig()


# ----------------------------------------------------------------------------------- loading
def _format_validation(exc: ValidationError) -> list[str]:
    out = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        out.append(f"{loc}: {err['msg']}")
    return out


def parse_yaml_model[M: BaseModel](model: type[M], name: str, data: bytes) -> M:
    try:
        raw: Any = safe_load(data)
    except (YamlError, UnicodeDecodeError) as exc:
        raise ConfigError({name: [f"YAML: {exc}"]}) from exc
    if not isinstance(raw, dict):
        raise ConfigError({name: ["top level must be a mapping"]})
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError({name: _format_validation(exc)}) from exc


@dataclass(frozen=True)
class ConfigBundle:
    policy: PolicyConfig
    targets: TargetsConfig
    egress: EgressConfig
    profile: ProfileConfig
    telegram: TelegramConfig = field(default_factory=TelegramConfig)


def parse_bundle(files: Mapping[str, bytes]) -> ConfigBundle:
    """Validate the snapshot files together; collects every file's errors. ``telegram.yaml`` is
    optional (schema defaults when absent); the other four are required."""
    missing = [n for n in REQUIRED_SNAPSHOT_FILES if n not in files]
    extra = [n for n in files if n not in SNAPSHOT_FILES]
    errors: dict[str, list[str]] = {n: ["file missing"] for n in missing}
    for n in extra:
        errors[n] = ["not a snapshot file"]
    parsed: dict[str, BaseModel] = {}
    models: dict[str, type[BaseModel]] = {
        "policy.yaml": PolicyConfig,
        "targets.yaml": TargetsConfig,
        "egress.yaml": EgressConfig,
        "profile.yaml": ProfileConfig,
        "telegram.yaml": TelegramConfig,
    }
    for name, model in models.items():
        if name not in files:
            continue
        try:
            parsed[name] = parse_yaml_model(model, name, files[name])
        except ConfigError as exc:
            errors.update(exc.errors)
    if errors:
        raise ConfigError(errors)
    policy = parsed["policy.yaml"]
    targets = parsed["targets.yaml"]
    egress = parsed["egress.yaml"]
    profile = parsed["profile.yaml"]
    telegram = parsed.get("telegram.yaml", TelegramConfig())
    assert isinstance(policy, PolicyConfig)
    assert isinstance(targets, TargetsConfig)
    assert isinstance(egress, EgressConfig)
    assert isinstance(profile, ProfileConfig)
    assert isinstance(telegram, TelegramConfig)
    return ConfigBundle(policy=policy, targets=targets, egress=egress, profile=profile, telegram=telegram)


def parse_models(data: bytes) -> ModelsConfig:
    return parse_yaml_model(ModelsConfig, "models.yaml", data)
