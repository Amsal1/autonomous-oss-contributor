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
from ossbot.common.project import PROJECT_URL
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
    owner_fix_rounds_max: NonNegInt = 5  # owner /fix rounds per PR, separate from watch_rounds_max (ADR-0003)
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
    # §12.3: the repo the PR disclosure footer links (an https URL without quotes or markup; forks set their own)
    project_url: Annotated[str, Field(max_length=200, pattern=r"^https://[A-Za-z0-9.-]+(/[A-Za-z0-9._~/-]*)?$")] = (
        PROJECT_URL
    )


CommitSigning = Literal["auto", "required", "off"]


class CommitPolicy(Strict):
    # owner addition 2026-10-21 (ADR-0006 addendum): SSH-sign the packaged commit with the optional
    # gatekeeper-held GITHUB_COMMIT_SIGNING_KEY. auto: sign when the key is set; required: /package
    # refuses without the key; off: never sign.
    signing: CommitSigning = "auto"


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


TOPIC_RE = r"^[a-z0-9][a-z0-9-]{0,49}$"  # GitHub topic rules: lowercase letters, digits, hyphens, <= 50
LABEL_RE = r"^[^\x00-\x1f\",]{1,50}$"  # no quote or comma: they would break the label:a,b OR list
LANGUAGE_RE = r"^[A-Za-z][A-Za-z0-9 +#.-]{0,39}$"
Topic = Annotated[str, Field(pattern=TOPIC_RE)]
Language = Annotated[str, Field(pattern=LANGUAGE_RE)]
IssueLabel = Annotated[str, Field(pattern=LABEL_RE)]
DEFAULT_OPEN_TOPICS: tuple[str, ...] = (
    "devops", "platform-engineering", "kubernetes", "terraform", "infrastructure-as-code", "gitops",
    "observability", "sre", "cloudformation", "aws", "ci-cd", "helm", "monitoring", "opentelemetry",
    "llm-observability",
)  # fmt: skip
DEFAULT_PREFERRED_LANGUAGES: tuple[str, ...] = (
    "Java", "JavaScript", "TypeScript", "Python", "Rust", "Go", "HCL", "CloudFormation",
)  # fmt: skip
DEFAULT_OPEN_LABELS: tuple[str, ...] = ("good first issue", "help wanted", "bug", "documentation", "docs")


def unique_topics(value: list[str]) -> list[str]:
    """Topics are unique after lower() (shared by the policy block and the /filters prefs)."""
    lowered = [t.lower() for t in value]
    if len(lowered) != len(set(lowered)):
        raise ValueError("topics must be unique")
    return value


def unique_languages(value: list[str]) -> list[str]:
    """Languages are unique case-insensitively (shared by the policy block and the /filters prefs)."""
    folded = [x.casefold() for x in value]
    if len(folded) != len(set(folded)):
        raise ValueError("languages must be unique (case-insensitive)")
    return value


class OpenDiscoveryPolicy(Strict):
    """Post-v1 open discovery (design §3.1): repos found by topic search beyond targets.yaml.

    Only ``enabled``, ``topics`` and ``languages_preferred`` are Telegram-tunable (``/filters``,
    ``discovery_prefs``); every count and budget is config-only (/reload)."""

    enabled: bool = True
    topics: Annotated[list[Topic], Field(min_length=1, max_length=30)] = Field(
        default_factory=lambda: list(DEFAULT_OPEN_TOPICS)
    )
    min_stars: Annotated[int, Field(ge=100, le=1_000_000)] = 1000
    pushed_within_days: Annotated[int, Field(ge=7, le=365)] = 60
    languages_preferred: Annotated[list[Language], Field(max_length=20)] = Field(
        default_factory=lambda: list(DEFAULT_PREFERRED_LANGUAGES)
    )
    language_weight: Annotated[float, Field(ge=0.0, le=30.0)] = 6.0
    new_repos_per_cycle: Annotated[int, Field(ge=1, le=20)] = 5
    topics_per_cycle: Annotated[int, Field(ge=1, le=15)] = 3
    reserved_slots: Annotated[int, Field(ge=0, le=3)] = 1
    repo_cooldown_days: Annotated[int, Field(ge=1, le=365)] = 14
    search_budget: Annotated[int, Field(ge=3, le=25)] = 10
    issue_labels: Annotated[list[IssueLabel], Field(min_length=1, max_length=10)] = Field(
        default_factory=lambda: list(DEFAULT_OPEN_LABELS)
    )

    @field_validator("topics")
    @classmethod
    def _topics_unique(cls, value: list[str]) -> list[str]:
        return unique_topics(value)

    @field_validator("languages_preferred")
    @classmethod
    def _languages_unique(cls, value: list[str]) -> list[str]:
        return unique_languages(value)

    @model_validator(mode="after")
    def _budget_fits(self) -> OpenDiscoveryPolicy:
        # the repo searches, one issue search and the reserved tier-1 mention checks fit one cycle
        need = self.topics_per_cycle + self.reserved_slots + 2
        if self.search_budget < need:
            raise ValueError(
                f"search_budget {self.search_budget} must be >= topics_per_cycle + reserved_slots + 2 ({need})"
            )
        return self


class DiscoveryPolicy(Strict):
    proactive: Literal[False] = False  # phase 2
    interval_hours: PosInt = 6
    top_n: PosInt = 5
    proposals_per_cycle: PosInt = 3  # §9.14: proposal cards per discovery cycle
    # Discovery fix 7 (2026-10-22): while fewer autonomous proposals than this reached gate 1 in 24 h and
    # none is on its way, the next cycle runs after interval_hours / 2. 0 = off; <= proposal.max_per_day.
    # Config-only (/reload), not a Telegram preference; it never raises max_per_day or the size ceiling.
    target_proposals_per_day: Annotated[int, Field(ge=0, le=50)] = 2
    # §9.4 (post-v1): a ranking boost for repos whose CI ran tests/lint on merged PRs; never a filter.
    # Config-only (/reload), not a Telegram-tunable discovery preference.
    ci_on_pr_weight: Annotated[float, Field(ge=0.0, le=30.0)] = 6.0
    ci_facts_ttl_hours: Annotated[int, Field(ge=1, le=720)] = 168
    # Owner addition (2026-10-21): skip a repo (seed or discovered) whose DEFAULT branch's latest commit
    # is older than this; read from the default branch's commits, not pushed_at (any branch push moves it).
    max_default_branch_age_days: Annotated[int, Field(ge=30, le=3650)] = 180
    open: OpenDiscoveryPolicy = OpenDiscoveryPolicy()


AiRule = Literal["allow", "skip"]


class AiPolicyRules(Strict):
    """§9.5 (owner decision, post-v1): skip a repo only on an explicit AI ban; a ban can't be allowed."""

    unstated: AiRule = "allow"  # no AI wording in any fetched repo doc
    unclear: AiRule = "allow"  # AI mentioned, neither banned nor allowed
    banned: Literal["skip"] = "skip"


class VetPolicy(Strict):
    ai_policy: AiPolicyRules = AiPolicyRules()


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
    proxy_logs_days: Annotated[int, Field(ge=30, le=3650)] = 30  # rotated squid access.log.* (design §13.5)
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
    commit: CommitPolicy = CommitPolicy()
    secrets: SecretsPolicy = SecretsPolicy()
    anomaly: AnomalyPolicy = AnomalyPolicy()
    validation: ValidationPolicy = ValidationPolicy()
    size: SizePolicy = SizePolicy()
    discovery_filters: DiscoveryFilters = DiscoveryFilters()
    proposal: ProposalPolicy = ProposalPolicy()
    discovery: DiscoveryPolicy = DiscoveryPolicy()
    vet: VetPolicy = VetPolicy()
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

    @model_validator(mode="after")
    def _target_within_daily_cap(self) -> PolicyConfig:
        if self.discovery.target_proposals_per_day > self.proposal.max_per_day:
            raise ValueError(
                f"discovery.target_proposals_per_day {self.discovery.target_proposals_per_day} exceeds"
                f" proposal.max_per_day {self.proposal.max_per_day}"
            )
        return self

    @model_validator(mode="after")
    def _reserved_slots_fit(self) -> PolicyConfig:
        if self.discovery.open.reserved_slots > self.discovery.proposals_per_cycle:
            raise ValueError(
                f"discovery.open.reserved_slots {self.discovery.open.reserved_slots} exceeds"
                f" discovery.proposals_per_cycle {self.discovery.proposals_per_cycle}"
            )
        return self


# ----------------------------------------------------------------------------------- targets
ValidationKind = Literal["static", "unit", "smoke"]
DEFAULT_VALIDATION: tuple[ValidationKind, ...] = ("static", "unit", "smoke")
TargetEgress = Literal["mode", "strict"]
# Post-v1 open discovery (design §8.5): repos outside targets.yaml resolve into this category; a kept
# repo ranks just ahead of a plain open candidate, both behind every configured category.
OPEN_CATEGORY = "open-discovery"
OPEN_PRIORITY = 5
KEPT_PRIORITY = 4


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
    # §12.3 (post-v1): "mode" follows the active egress mode; "strict" keeps the allowlist pool for
    # fetch/validate jobs of the repo in every mode. None = "mode".
    egress: TargetEgress | None = None


class AiPolicyOverride(Strict):
    """Per-repo override of ``policy.vet.ai_policy`` (§9.5); there is no ``banned`` key (a ban always skips)."""

    unstated: AiRule | None = None
    unclear: AiRule | None = None


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
    ai_policy: AiPolicyOverride | None = None
    egress_extra: list[Annotated[str, Field(pattern=HOST_RE)]] = Field(default_factory=list)
    disclosure_template: str | None = None
    egress: TargetEgress | None = None  # §12.3; None = defaults.egress (else "mode")


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
    ai_unstated: str | None = None  # per-repo ``ai_policy`` override (None = policy.vet.ai_policy)
    ai_unclear: str | None = None
    egress: str = "mode"  # §12.3: "mode" | "strict" (entry, else defaults.egress, else "mode")


def effective_ai_rules(policy: PolicyConfig, target: ResolvedTarget | None) -> AiPolicyRules:
    """``policy.vet.ai_policy`` with the target's overrides applied; ``ai_policy_override: allowed``
    allows both ``unstated`` and ``unclear``. ``banned`` is always ``skip``."""
    rules = policy.vet.ai_policy
    if target is None:
        return rules
    if target.ai_policy_override == "allowed":
        return AiPolicyRules(unstated="allow", unclear="allow")
    return AiPolicyRules(
        unstated="skip" if (target.ai_unstated or rules.unstated) == "skip" else "allow",
        unclear="skip" if (target.ai_unclear or rules.unclear) == "skip" else "allow",
    )


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
                ai_unstated=e.ai_policy.unstated if e.ai_policy else None,
                ai_unclear=e.ai_policy.unclear if e.ai_policy else None,
                egress=e.egress or d.egress or "mode",
            )
    return None


def discovered_target(targets: TargetsConfig, repo: str, *, kept: bool, egress_strict: bool) -> ResolvedTarget | None:
    """A repo outside targets.yaml that passed the open-discovery checks (design §8.5), or None if denied.

    Seed defaults throughout (``enabled``, ``size_class``, ``ai_policy_override``, labels, validation,
    ``cla_signed``, ``egress_extra``: the same merge ``resolve_target`` applies; review-3 L2); no per-repo
    host, AI override or disclosure template. The caller checks that ``repo`` isn't a config target."""
    if targets.is_denied(repo):
        return None
    d = targets.defaults
    return ResolvedTarget(
        repo=repo,
        category=OPEN_CATEGORY,
        priority=KEPT_PRIORITY if kept else OPEN_PRIORITY,
        enabled=d.enabled,
        discovery=True,
        labels=tuple(dict.fromkeys(d.labels)),
        skip_labels=tuple(d.skip_labels),
        clone_depth=d.clone_depth,
        size_class=d.size_class,
        validation=tuple(d.validation),
        cla_signed=d.cla_signed,
        ai_policy_override=d.ai_policy_override,
        egress_extra=tuple(d.egress_extra),
        disclosure_template=None,
        egress="strict" if egress_strict else (d.egress or "mode"),
    )


def helm_hosts(targets: TargetsConfig) -> list[str]:
    """Every ``egress_extra`` host of targets.yaml (defaults first): the ``pkg_helm`` category (§11.2)."""
    hosts = list(targets.defaults.egress_extra)
    for category in targets.categories:
        for repo in category.repos:
            hosts.extend(repo.egress_extra)
    return hosts


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
# grantees. runner and gatekeeper are not keys, so data can never widen what the trusted writers can
# reach. fetch and validate (repo code) are keys only for the owner's mode sandbox_open (ADR-0023):
# the validator below admits them there only, with web_public only, and the same list for both (they
# share the fetch pool); strict repos keep the base list in every mode. The renderer asserts the rest.
ModeGrantee = Literal["research", "hermes", "agent-llm", "catalog", "fetch", "validate"]
REPO_CODE_GRANTEES: tuple[ModeGrantee, ...] = ("fetch", "validate")
REPO_CODE_GRANT_MODE = "sandbox_open"
TELEGRAPH_CATEGORY = "telegraph"  # DESIGN §14.12.8: the gatekeeper's opt-in Telegraph egress


class EgressAnomaly(Strict):
    """Per-job egress limits of fetch/validate jobs (design §13.4); config-only, never Telegram-tunable."""

    max_hosts: Annotated[int, Field(ge=5, le=1000)] = 60
    max_upload_mb: Annotated[int, Field(ge=1, le=10_240)] = 50
    max_download_mb: Annotated[int, Field(ge=64, le=102_400)] = 6144


class EgressConfig(Strict):
    version: Literal[1]
    categories: dict[str, list[Annotated[str, Field(pattern=HOST_RE)]]]
    denied_by_default: list[str] = Field(default_factory=list)
    sandbox_profiles: dict[SandboxProfileName, list[str]]
    host_callers: dict[HostCallerName, HostCaller]
    always_denied: AlwaysDenied
    mode_default: EgressMode = "open"
    modes: dict[EgressMode, dict[ModeGrantee, list[str]]] = Field(default_factory=dict)
    anomaly: EgressAnomaly = EgressAnomaly()

    def repo_code_web(self, mode: str) -> bool:
        """Whether ``mode`` grants fetch/validate web_public (the validator allows it in sandbox_open only)."""
        return any(
            m == mode and "web_public" in grants.get(g, [])
            for m, grants in self.modes.items()
            for g in REPO_CODE_GRANTEES
        )

    def repo_code_change(self, before: str, after: str) -> str:
        """``+web_public`` / ``-web_public`` (a minus sign, U+2212, like egress_changes) when a switch changes
        what repo code reaches, else ``unchanged``."""
        was, now = self.repo_code_web(before), self.repo_code_web(after)
        return "unchanged" if was == now else "+web_public" if now else "−web_public"

    @model_validator(mode="after")
    def _references_exist(self) -> EgressConfig:
        missing_modes = [m for m in EGRESS_MODES if m not in self.modes]
        if missing_modes:
            raise ValueError(f"modes must define every egress mode; missing {missing_modes}")
        for mode, grants in self.modes.items():
            code = {g: grants[g] for g in REPO_CODE_GRANTEES if g in grants}
            if not code:
                continue
            if mode != REPO_CODE_GRANT_MODE:
                raise ValueError(f"fetch/validate get grants only in egress mode {REPO_CODE_GRANT_MODE} (not {mode})")
            if any(cats != ["web_public"] for cats in code.values()):
                raise ValueError("fetch/validate may be granted web_public only")
            if len(code) != len(REPO_CODE_GRANTEES):
                raise ValueError("fetch and validate share a squid pool: grant both the same list")
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
    # How plain text typed in All is handled (§14.10): topic_session pairs the implicit topic with its
    # text and keeps it as a chat session; legacy is the pre-post-v1 behaviour. Presentation only;
    # revert = set `legacy` and /reload.
    all_text_mode: Literal["topic_session", "legacy"] = "topic_session"
    # Tappable command shortcuts (inline buttons, which send no message) on the dashboard, /help and
    # button-less command replies in All. Presentation only; revert = set false and /reload.
    command_shortcuts: bool = True


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
