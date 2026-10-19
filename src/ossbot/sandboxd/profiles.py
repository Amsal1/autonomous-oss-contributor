"""Fixed per-profile templates and the JobSpec API model (DESIGN §10.2, §10.3, §10.4, §15.3).

The caller chooses a JobSpec profile, entrypoint and typed args; everything that becomes a
docker flag (image, network, mounts, tmpfs mode, env names, key) comes from the tables here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ossbot.common import github_mcp_tools
from ossbot.common.config import MODEL_ID_RE, REPO_RE, Effort
from ossbot.common.schemas import BASE_REF_RE, SHA1_RE

JobProfile = Literal["fetch", "research", "coder", "validate", "package", "catalog", "extract", "code", "stack"]
JobEntrypoint = Literal[
    "fetch", "kiro", "validate", "package", "unbundle", "list_models", "extract_text", "run_code", "probe"
]
RepoMode = Literal["none", "snapshot", "rw"]
ResearchRole = Literal["vetter", "scout", "planner", "reviewer", "summarizer"]  # scout: decision B8 (CR-004)
KiroRole = Literal["vetter", "scout", "planner", "reviewer", "summarizer", "coder"]

RUN_ID_RE = r"^[a-z0-9][a-z0-9_-]{0,30}$"
CID_RE = r"^(c_[0-9A-Z]{10}|sys-catalog|sys-chat)$"
INPUT_NAME_RE = r"^[a-z0-9_.-]{1,64}$"
PROMPT_FILE_RE = r"^[a-z0-9_-]{1,64}\.md$"
CATALOG_CID = "sys-catalog"
CHAT_CID = "sys-chat"  # the assistant's PDF text (extract_text, §14.12.5) and code jobs (run_code, §15.6)
CHAT_PROFILES = frozenset({"extract", "code"})
EXTRACT_INPUT = "doc.pdf"
CODE_SOURCES = {"python": "main.py", "bash": "main.sh"}
CODE_MAX_INPUTS = 6  # the source plus at most 5 attachments
CODE_MAX_TIMEOUT_S = 120
MAX_CPUS = 8.0
MAX_MEMORY_MB = 6144
MIN_MEMORY_MB = 64


@dataclass(frozen=True)
class ContainerTemplate:
    """One container shape. ``network`` is ``none`` or a network profile (see ``POOLS``)."""

    profile: str  # the ossbot.profile label
    image: str  # sandbox-full | sandbox-llm
    network: str
    tmp_exec: bool  # M2: exec on /tmp only for code-running profiles
    key: bool  # gets KIRO_API_KEY (name-only --env)
    llm_home: bool  # tmpfs /home/agent HOME + empty tmpfs cwd
    exec_volume: bool  # /run/exec socket volume (split coder)


TEMPLATES: dict[str, ContainerTemplate] = {
    "fetch": ContainerTemplate("fetch", "sandbox-full", "fetch", True, False, False, False),
    "validate": ContainerTemplate("validate", "sandbox-full", "validate", True, False, False, False),
    "package": ContainerTemplate("package", "sandbox-full", "none", True, False, False, False),
    "research": ContainerTemplate("research", "sandbox-llm", "research", False, True, True, False),
    "agent-llm": ContainerTemplate("agent-llm", "sandbox-llm", "llm", False, True, True, True),
    "agent-exec": ContainerTemplate("agent-exec", "sandbox-full", "none", True, False, False, True),
    "catalog": ContainerTemplate("catalog", "sandbox-llm", "catalog", False, True, True, False),
    # pdftotext on an owner's PDF: no network, no key, no repo, noexec /tmp (§14.12.5)
    "extract": ContainerTemplate("extract", "sandbox-full", "none", False, False, False, False),
    # the assistant's code jobs (§15.6, owner decision F007-R1-2): Hermes' python/bash on the owner's
    # attachments; no network, no key, no repo, exec /tmp as the scratch dir
    "code": ContainerTemplate("code", "sandbox-full", "none", True, False, False, False),
    # internal helpers (never submitted through POST /v1/jobs)
    "stage-in": ContainerTemplate("stage-in", "sandbox-full", "none", False, False, False, False),
    "snapshot": ContainerTemplate("snapshot", "sandbox-full", "none", False, False, False, False),
    "probe": ContainerTemplate("probe", "sandbox-full", "none", False, False, False, False),
}

# /28 pools per network profile (§10.3). Profiles sharing a pool share a squid class, so their
# egress categories must be identical (the squid renderer checks this).
POOLS: dict[str, str] = {
    "fetch": "10.213.0.0/18",
    "validate": "10.213.0.0/18",
    "research": "10.213.64.0/18",
    "llm": "10.213.128.0/18",
    "catalog": "10.213.128.0/18",
}
# network profile -> the egress.yaml sandbox profile whose categories apply
EGRESS_PROFILE: dict[str, str] = {
    "fetch": "fetch", "validate": "validate", "research": "research", "llm": "agent-llm", "catalog": "catalog",
}  # fmt: skip

ROLE_REPO_MODE: dict[str, RepoMode] = {
    "vetter": "none", "summarizer": "none", "scout": "snapshot", "planner": "snapshot", "reviewer": "snapshot",
    "coder": "rw",
}  # fmt: skip
PROFILE_REPO_MODE: dict[str, RepoMode] = {
    "fetch": "rw",
    "validate": "rw",
    "package": "rw",
    "coder": "rw",
    "catalog": "none",
    "extract": "none",
    "code": "none",
}
PROFILE_ENTRYPOINTS: dict[str, frozenset[str]] = {
    "fetch": frozenset({"fetch"}),
    "research": frozenset({"kiro"}),
    "coder": frozenset({"kiro"}),
    "validate": frozenset({"validate"}),
    "package": frozenset({"package", "unbundle"}),
    "catalog": frozenset({"list_models"}),
    "extract": frozenset({"extract_text"}),
    "code": frozenset({"run_code"}),
}
PROFILE_MAX_TIMEOUT: dict[str, int] = {
    "fetch": 3600, "research": 3600, "coder": 7200, "validate": 7200, "package": 900, "catalog": 300,
    "extract": 300, "code": CODE_MAX_TIMEOUT_S + 60,
}  # fmt: skip
# Fixed per role, never caller-supplied (§15.3). [VERIFY §23 item 5: exact V3 internal tool ids]
_GITHUB_READ = ",".join(github_mcp_tools.kiro_tool_ids())  # GHMCP-2: the read-only GitHub MCP tools
TRUST_TOOLS: dict[str, str] = {
    "vetter": "read,web_search,web_fetch",
    "scout": f"read,web_search,web_fetch,{_GITHUB_READ}",  # shallow snapshot ro + web per egress mode (§9.5a)
    "planner": "read,web_search,web_fetch",
    "reviewer": f"read,web_search,web_fetch,{_GITHUB_READ}",
    "summarizer": "read",
    "coder": "@ossbot-exec/read_file,@ossbot-exec/write_file,@ossbot-exec/list_dir,@ossbot-exec/run",
}
# agent-llm runs only kiro-cli + the MCP relay: fixed small limits independent of the JobSpec.
AGENT_LLM_CPUS = 2.0
AGENT_LLM_MEMORY_MB = 1024


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class FetchArgs(Strict):
    repo: Annotated[str, Field(pattern=REPO_RE)]
    base_ref: Annotated[str, Field(pattern=BASE_REF_RE)] | None = None
    base_sha: Annotated[str, Field(pattern=SHA1_RE)] | None = None
    clone_depth: Annotated[int, Field(ge=1, le=10000)] = 200
    # CR-004 scout: a shallow clone without dependency prefetch (no repo code runs), replaced by
    # the real fetch after Start work (fetch.sh removes the scout clone first).
    prefetch: bool = True

    @model_validator(mode="after")
    def _sha_needs_ref(self) -> FetchArgs:
        if self.base_sha is not None and self.base_ref is None:
            raise ValueError("base_sha requires base_ref")
        if not self.prefetch and (self.clone_depth != 1 or self.base_ref is not None):
            raise ValueError("the scout clone (prefetch false) is depth 1 of the default branch")
        return self


class KiroArgs(Strict):
    role: KiroRole
    prompt_file: Annotated[str, Field(pattern=PROMPT_FILE_RE)]
    agent: str | None = None
    model: Annotated[str, Field(pattern=MODEL_ID_RE)]
    effort: Effort

    @model_validator(mode="after")
    def _agent_from_role(self) -> KiroArgs:
        if self.agent is not None and self.agent != f"ossbot-{self.role}":
            raise ValueError("agent is fixed by role")
        return self


class ValidateArgs(Strict):
    plan_file: Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,64}\.json$")] = "validate.json"


class PackageArgs(Strict):
    rev: Annotated[int, Field(ge=1, le=10_000)]
    round_base: Annotated[str, Field(pattern=SHA1_RE)]
    package_ts: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")]
    name: Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^<>\n\r\x00]+$")]
    email: Annotated[str, Field(max_length=254, pattern=r"^[^@\s<>]+@[^@\s<>]+$")]


class UnbundleArgs(Strict):
    mode: Literal["fix_round", "import"]
    bundle_ref: Annotated[str, Field(pattern=r"^refs/heads/[A-Za-z0-9._/-]{1,100}$")]
    checkout_sha: Annotated[str, Field(pattern=SHA1_RE)]
    rev: Annotated[int, Field(ge=1, le=10_000)]


class ListModelsArgs(Strict):
    pass


class ExtractTextArgs(Strict):
    pass


class RunCodeArgs(Strict):
    language: Literal["python", "bash"]
    timeout_s: Annotated[int, Field(ge=1, le=CODE_MAX_TIMEOUT_S)] = CODE_MAX_TIMEOUT_S


ARGS_MODELS: dict[str, type[Strict]] = {
    "fetch": FetchArgs,
    "kiro": KiroArgs,
    "validate": ValidateArgs,
    "package": PackageArgs,
    "unbundle": UnbundleArgs,
    "list_models": ListModelsArgs,
    "extract_text": ExtractTextArgs,
    "run_code": RunCodeArgs,
}


class JobSpec(BaseModel):
    """``POST /v1/jobs`` body (§10.2). ``args`` is validated per entrypoint into ``typed_args``."""

    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: Annotated[str, Field(pattern=RUN_ID_RE)]
    cid: Annotated[str, Field(pattern=CID_RE)]
    profile: JobProfile
    entrypoint: JobEntrypoint
    args: dict[str, Any] = Field(default_factory=dict)
    repo_mode: RepoMode
    inputs: dict[Annotated[str, Field(pattern=INPUT_NAME_RE)], Annotated[str, Field(min_length=1, max_length=512)]] = (
        Field(default_factory=dict, max_length=64)
    )
    timeout_s: Annotated[int, Field(ge=1)]
    cpus: Annotated[float, Field(gt=0, le=MAX_CPUS)] = 2.0
    memory_mb: Annotated[int, Field(ge=MIN_MEMORY_MB, le=MAX_MEMORY_MB)] = 4096


class SpecError(ValueError):
    """A JobSpec that is well-formed JSON but violates the per-profile rules (HTTP 400)."""


def _parse_args(entrypoint: str, args: dict[str, Any]) -> Strict:
    model = ARGS_MODELS.get(entrypoint)
    if model is None:
        raise SpecError(f"entrypoint {entrypoint} is internal")
    try:
        return model.model_validate(args)
    except ValidationError as exc:
        raise SpecError(f"args: {exc.errors()[0]['msg']}") from exc


@dataclass(frozen=True)
class ValidatedJob:
    spec: JobSpec
    args: Strict

    @property
    def role(self) -> str | None:
        return self.args.role if isinstance(self.args, KiroArgs) else None


def validate_job(spec: JobSpec) -> ValidatedJob:
    """Every rule that does not need the catalog, the probe table or the control flags."""
    if spec.profile == "stack":
        raise SpecError("profile stack is phase 2 (validation.stack.enabled is false)")
    if spec.entrypoint == "probe":
        raise SpecError("probe jobs are created only by POST /v1/probes/run")
    if spec.entrypoint not in PROFILE_ENTRYPOINTS[spec.profile]:
        raise SpecError(f"entrypoint {spec.entrypoint} is not allowed in profile {spec.profile}")
    args = _parse_args(spec.entrypoint, spec.args)
    if isinstance(args, KiroArgs):
        expected_role_profile = "coder" if args.role == "coder" else "research"
        if spec.profile != expected_role_profile:
            raise SpecError(f"role {args.role} runs in profile {expected_role_profile}")
        expected_mode = ROLE_REPO_MODE[args.role]
    else:
        expected_mode = PROFILE_REPO_MODE[spec.profile]
    if spec.repo_mode != expected_mode:
        raise SpecError(f"repo_mode must be {expected_mode} for this profile/role")
    if (spec.profile == "catalog") != (spec.cid == CATALOG_CID):
        raise SpecError("catalog jobs use the pseudo-cid sys-catalog, and only they do")
    if spec.profile == "catalog" and spec.inputs:
        raise SpecError("catalog jobs take no inputs")
    if (spec.profile in CHAT_PROFILES) != (spec.cid == CHAT_CID):
        raise SpecError("extract and code jobs use the pseudo-cid sys-chat, and only they do")
    if spec.profile == "extract" and set(spec.inputs) != {EXTRACT_INPUT}:
        raise SpecError(f"extract jobs take exactly the input {EXTRACT_INPUT}")
    if isinstance(args, RunCodeArgs):
        source = CODE_SOURCES[args.language]
        others = set(spec.inputs) - {source}
        if source not in spec.inputs or len(spec.inputs) > CODE_MAX_INPUTS or set(CODE_SOURCES.values()) & others:
            raise SpecError(f"code jobs take {source} and at most {CODE_MAX_INPUTS - 1} attachments")
        if spec.timeout_s < args.timeout_s + 10:
            raise SpecError("code jobs need timeout_s at least the code timeout + 10 s")
    if spec.timeout_s > PROFILE_MAX_TIMEOUT[spec.profile]:
        raise SpecError(f"timeout_s exceeds the {spec.profile} maximum")
    return ValidatedJob(spec=spec, args=args)


def cid_slug(cid: str) -> str:
    """Lowercase form for labels and volume names (``^[a-z0-9_-]{1,40}$``)."""
    slug = cid.lower()
    if re.fullmatch(r"[a-z0-9_-]{1,40}", slug) is None:
        raise SpecError("cid")
    return slug


def network_name(cid: str, network_profile: str) -> str:
    """``ossbot-t-<cid>-<profile>`` (network names allow no underscore)."""
    return f"ossbot-t-{cid_slug(cid).replace('_', '-')}-{network_profile}"
