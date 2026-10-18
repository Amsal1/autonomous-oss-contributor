"""Config schemas (pydantic v2, ``extra="forbid"``, strict) and loaders.

Security-relevant files (policy, targets, egress, profile) reach components only through the
gatekeeper's config snapshot (DESIGN §7.9). ``models.yaml`` is the only live-read file (§16).
Flags whose non-v1 values are phase-2 features accept only the v1 value (PLAN "Deferred").
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ossbot.common.yamlio import YamlError, safe_load

SNAPSHOT_FILES: tuple[str, ...] = ("egress.yaml", "policy.yaml", "profile.yaml", "targets.yaml")
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
    max_files: PosInt = 25
    max_changed_lines: PosInt = 600
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


class DiscoveryPolicy(Strict):
    proactive: Literal[False] = False  # phase 2
    interval_hours: PosInt = 6
    top_n: PosInt = 5


class TelegramPolicy(Strict):
    mode: Literal["single"] = "single"  # 'dual' is phase 2


class BackupPolicy(Strict):
    include_secrets: bool = False
    keep: PosInt = 8


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
    discovery: DiscoveryPolicy = DiscoveryPolicy()
    telegram: TelegramPolicy = TelegramPolicy()
    backup: BackupPolicy = BackupPolicy()


# ----------------------------------------------------------------------------------- targets
ValidationKind = Literal["static", "unit", "smoke"]
DEFAULT_VALIDATION: tuple[ValidationKind, ...] = ("static", "unit", "smoke")


class TargetDefaults(Strict):
    enabled: bool = True
    labels: list[str] = Field(default_factory=list)
    skip_labels: list[str] = Field(default_factory=list)
    clone_depth: PosInt = 200
    max_changed_lines: PosInt = 400
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
    max_changed_lines: PosInt | None = None
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
    max_changed_lines: int
    validation: tuple[str, ...]
    cla_signed: bool
    ai_policy_override: str | None
    egress_extra: tuple[str, ...]
    disclosure_template: str | None


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
                max_changed_lines=e.max_changed_lines or d.max_changed_lines,
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


class EgressConfig(Strict):
    version: Literal[1]
    categories: dict[str, list[Annotated[str, Field(pattern=HOST_RE)]]]
    denied_by_default: list[str] = Field(default_factory=list)
    sandbox_profiles: dict[SandboxProfileName, list[str]]
    host_callers: dict[HostCallerName, HostCaller]
    always_denied: AlwaysDenied

    @model_validator(mode="after")
    def _references_exist(self) -> EgressConfig:
        known = set(self.categories) | {"web_public", "kiro_idc_login", "pkg_helm"}
        refs = [*self.denied_by_default]
        for cats in self.sandbox_profiles.values():
            refs.extend(cats)
        for caller in self.host_callers.values():
            refs.extend(caller.categories)
        unknown = sorted(set(refs) - known)
        if unknown:
            raise ValueError(f"unknown egress categories: {unknown}")
        ports = [c.port for c in self.host_callers.values()]
        if len(ports) != len(set(ports)):
            raise ValueError("host caller ports must be distinct")
        return self


# ----------------------------------------------------------------------------------- profile
class TelegramProfile(Strict):
    user_id: NonNegInt = 0  # 0 = not configured; the Telegram front-end stays disabled


class KiroProfile(Strict):
    idc_start_url: Annotated[str, Field(pattern=r"^https://[A-Za-z0-9.-]+(/[^\s]*)?$")]
    idc_region: Annotated[str, Field(pattern=r"^[a-z]{2}-[a-z]+-\d$")]


class BackupProfile(Strict):
    age_recipients: list[str] = Field(default_factory=list)


class InterestsProfile(Strict):
    summary: str = ""
    categories_priority: dict[str, int] = Field(default_factory=dict)
    prior_contributions: list[Annotated[str, Field(pattern=REPO_RE)]] = Field(default_factory=list)


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
RoleName = Literal["chat", "planner", "vetter", "coder", "reviewer", "summarizer"]
ROLE_NAMES: tuple[str, ...] = ("chat", "planner", "vetter", "coder", "reviewer", "summarizer")
ModelRef = Annotated[str, Field(pattern=MODEL_ID_RE)]


class DefaultModel(Strict):
    model: ModelRef
    effort: Effort = "high"


class RoleModel(Strict):
    model: ModelRef  # 'default' or a catalog id
    effort: Effort | None = None
    require_distinct_from: RoleName | None = None


class RolesConfig(Strict):
    chat: RoleModel
    planner: RoleModel
    vetter: RoleModel
    coder: RoleModel
    reviewer: RoleModel
    summarizer: RoleModel


class CatalogConfig(Strict):
    refresh_hours: PosInt = 24
    notify_new_models: bool = True


class ModelsConfig(Strict):
    version: Literal[1]
    default: DefaultModel
    roles: RolesConfig
    fallbacks: list[ModelRef] = Field(default_factory=list)
    catalog: CatalogConfig = CatalogConfig()


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


def parse_bundle(files: Mapping[str, bytes]) -> ConfigBundle:
    """Validate the four snapshot files together; collects every file's errors."""
    missing = [n for n in SNAPSHOT_FILES if n not in files]
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
    assert isinstance(policy, PolicyConfig)
    assert isinstance(targets, TargetsConfig)
    assert isinstance(egress, EgressConfig)
    assert isinstance(profile, ProfileConfig)
    return ConfigBundle(policy=policy, targets=targets, egress=egress, profile=profile)


def parse_models(data: bytes) -> ModelsConfig:
    return parse_yaml_model(ModelsConfig, "models.yaml", data)
