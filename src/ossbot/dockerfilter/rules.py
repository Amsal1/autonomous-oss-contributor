"""Docker API allowlist of ``ossbot-dockerfilter`` (DESIGN §10.8, M4, N5, R4-N5).

Pure and deterministic: ``decide()`` takes a parsed request, a ``Lookup`` (inspects the filter
performs itself on the real socket) and the ``FilterPolicy`` (image digest allowlist, control
dir) and returns a ``Decision``. Nothing a caller sends is trusted: ids are resolved through
``Lookup``, JSON bodies are parsed with duplicate keys rejected, and every create-body key that
is not in the specification must be absent, ``null`` or its type's zero value.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs

from ossbot.common.control_flags import Blocked, require_running
from ossbot.common.images import load_image_pins

MAX_JSON_BODY = 256 * 1024
API_VERSION_RE = re.compile(r"^/v1\.\d{1,3}(?=/)")
OBJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
CONTAINER_NAME_RE = re.compile(r"^ossbot-[a-z0-9_-]{1,80}$")
LABEL_VALUE_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
NETWORK_RE = re.compile(r"^ossbot-t-[a-z0-9-]+$")
VOLUME_RE = re.compile(r"^ossbot-(task|out|in|exec|ro)-[a-z0-9_-]+$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
IMAGE_RE = re.compile(r"^ossbot/(sandbox-full|sandbox-llm|sandbox-android)@sha256:([0-9a-f]{64})$")
ENTRYPOINT_CMD_RE = re.compile(r"^/opt/ossbot/entrypoints/([a-z_]{1,32})\.sh$")

PROFILES: frozenset[str] = frozenset(
    {"fetch", "research", "agent-llm", "agent-exec", "validate", "package", "catalog", "extract", "code",
     "snapshot", "stage-in", "probe", "stack"}
)  # fmt: skip
# R4-N5: entrypoint name bound to ossbot.profile; probe.sh is accepted in every profile except
# stage-in and stack (22.5 smoke tests run it once per profile).
CMD_BY_PROFILE: Mapping[str, frozenset[str]] = {
    "fetch": frozenset({"fetch"}),
    "research": frozenset({"kiro"}),
    "agent-llm": frozenset({"kiro"}),
    "agent-exec": frozenset({"exec"}),
    "validate": frozenset({"validate"}),
    "package": frozenset({"package", "unbundle"}),
    "catalog": frozenset({"list_models"}),
    "extract": frozenset({"extract_text"}),
    "code": frozenset({"run_code"}),
    "snapshot": frozenset({"snapshot"}),
    "probe": frozenset({"probe"}),
}
KEY_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {("research", "kiro"), ("agent-llm", "kiro"), ("catalog", "list_models")}
)
# the read token's roles, kept here on purpose (the filter does not import sandboxd or common)
GITHUB_READ_ROLES: frozenset[str] = frozenset({"scout", "reviewer"})
# code: the validate rules (exec /tmp) with the network forced to none (owner decision F007-R1-2)
TMP_EXEC_PROFILES: frozenset[str] = frozenset({"fetch", "validate", "agent-exec", "package", "code"})
NETWORK_NONE_PROFILES: frozenset[str] = frozenset({"agent-exec", "package", "extract", "code", "snapshot", "stage-in"})
LLM_IMAGE_PROFILES: frozenset[str] = frozenset({"research", "agent-llm", "catalog"})
CATALOG_NETWORK = "ossbot-t-sys-catalog-catalog"
STAGE_IN_CMD = ["/bin/true"]
MOUNT_TARGETS: frozenset[str] = frozenset(
    {"/work", "/work/repo", "/io/input", "/io/output", "/run/exec", "/in", "/snap"}
)
WORKDIRS: frozenset[str] = frozenset({"/work/repo", "/home/agent/cwd", "/"})
ENV_FIXED: frozenset[str] = frozenset(
    {"HOME", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ROLE", "PROMPT_FILE", "MODEL", "EFFORT", "TRUST_TOOLS",
     "REQUIRE_MCP"}
)  # fmt: skip
TMPFS_PATHS: frozenset[str] = frozenset({"/tmp", "/home/agent"})  # noqa: S108 - container paths
TMPFS_FLAGS: frozenset[str] = frozenset({"rw", "nosuid", "nodev", "exec", "noexec", "mode=1777"})
TMPFS_MAX = 2 * 1024**3
MIB = 1024 * 1024
MEMORY_MIN, MEMORY_MAX = 64 * MIB, 6 * 1024 * MIB
NANOCPUS_MIN, NANOCPUS_MAX = 100_000_000, 8_000_000_000
SHM_MAX = 64 * MIB
ULIMITS = [{"Name": "nofile", "Soft": 65536, "Hard": 65536}]
ENV_VALUE_MAX = 4096
TASK_POOL = ipaddress.IPv4Network("10.213.0.0/16")
PROXY_CONTAINER = "ossbot-proxy"
KILL_SIGNALS: frozenset[str] = frozenset({"SIGKILL", "KILL", "9", "SIGTERM", "TERM", "15"})
WAIT_CONDITIONS: frozenset[str] = frozenset({"not-running", "next-exit", "removed"})

CREATE_HOSTCONFIG_EMPTY: tuple[str, ...] = (
    "Binds", "VolumesFrom", "Links", "Devices", "DeviceRequests", "DeviceCgroupRules", "Sysctls", "ExtraHosts",
    "CapAdd", "GroupAdd", "PortBindings", "DnsOptions", "StorageOpt", "Annotations",
)  # fmt: skip


class Denied(Exception):
    """A request outside the allowlist; ``reason`` is logged and returned in the 403 body."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Request:
    method: str
    path: str  # without the query string
    query: Mapping[str, list[str]] = field(default_factory=dict)
    body: bytes = b""


@dataclass(frozen=True)
class ContainerFacts:
    id: str
    name: str  # without the leading "/"
    labels: Mapping[str, str]
    status: str  # State.Status: created | running | exited | ...

    @property
    def sandbox(self) -> bool:
        return self.labels.get("ossbot.sandbox") == "1"

    @property
    def profile(self) -> str:
        return self.labels.get("ossbot.profile", "")


@dataclass(frozen=True)
class NetworkFacts:
    id: str
    name: str


class Lookup(Protocol):
    def container(self, ref: str) -> ContainerFacts | None: ...
    def network(self, ref: str) -> NetworkFacts | None: ...


@dataclass(frozen=True)
class FilterPolicy:
    """``image_digests``: image name (``sandbox-full``) -> allowed sha256 hex digests."""

    image_digests: Mapping[str, frozenset[str]]
    control_dir: Path


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    operation: str

    @property
    def status(self) -> int:
        return 200 if self.allow else 403


# ------------------------------------------------------------------------------- helpers
def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise Denied(f"duplicate_key:{key}")
        out[key] = value
    return out


def _reject_constant(name: str) -> Any:
    raise Denied(f"bad_json_constant:{name}")


def parse_json(body: bytes, *, allow_empty: bool = False) -> Any:
    if not body.strip():
        if allow_empty:
            return None
        raise Denied("body_required")
    if len(body) > MAX_JSON_BODY:
        raise Denied("body_too_large")
    try:
        return json.loads(body.decode("utf-8"), object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
    except Denied:
        raise
    except (ValueError, UnicodeDecodeError) as exc:
        raise Denied("bad_json") from exc


def is_zero(value: Any) -> bool:
    if value is None or value is False:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return value == 0
    if isinstance(value, str | list | dict):
        return len(value) == 0
    return False


def _obj(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Denied(f"type:{path}")
    return value


def _check_rest_zero(obj: Mapping[str, Any], known: set[str], path: str) -> None:
    for key, value in obj.items():
        if key not in known and not is_zero(value):
            raise Denied(f"unknown_key:{path}{key}")


def _eq(obj: Mapping[str, Any], key: str, expected: Any, path: str) -> None:
    value = obj.get(key)
    if value != expected or type(value) is not type(expected):
        raise Denied(f"value:{path}{key}")


def _zero_or(obj: Mapping[str, Any], key: str, allowed: set[Any], path: str) -> Any:
    value = obj.get(key)
    if is_zero(value) and value not in allowed:
        return value
    if not isinstance(value, str | int) or isinstance(value, bool) or value not in allowed:
        raise Denied(f"value:{path}{key}")
    return value


def _int_in(obj: Mapping[str, Any], key: str, lo: int, hi: int, path: str) -> int:
    value = obj.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise Denied(f"value:{path}{key}")
    return value


def _q(req: Request, name: str) -> str | None:
    values = req.query.get(name)
    if not values:
        return None
    if len(values) != 1:
        raise Denied(f"query_repeated:{name}")
    return values[0]


def _check_query(req: Request, allowed: set[str]) -> None:
    for key in req.query:
        if key not in allowed:
            raise Denied(f"query:{key}")


def _bool_query(req: Request, name: str, *, required: bool | None = None) -> None:
    value = _q(req, name)
    if value is None:
        if required is True:
            raise Denied(f"query_required:{name}")
        return
    if value not in {"0", "1", "true", "false"}:
        raise Denied(f"query:{name}")
    if required is not None and (value in {"1", "true"}) is not required:
        raise Denied(f"query:{name}")


def _flags(policy: FilterPolicy, what: str) -> None:
    try:
        require_running(policy.control_dir, what)
    except Blocked as exc:
        raise Denied(f"{exc.flags.state}:{what}") from exc


# ----------------------------------------------------------------------- create body
def _parse_size(text: str) -> int:
    match = re.fullmatch(r"(\d{1,12})([kmg]?)", text.lower())
    if match is None:
        raise Denied("tmpfs_size")
    number, unit = match.groups()
    return int(number) * {"": 1, "k": 1024, "m": MIB, "g": 1024**3}[unit]


def _check_tmpfs(tmpfs: Any, profile: str) -> None:
    if is_zero(tmpfs):
        return
    mounts = _obj(tmpfs, "HostConfig.Tmpfs")
    for target, options in mounts.items():
        if target not in TMPFS_PATHS or not isinstance(options, str):
            raise Denied(f"tmpfs_path:{target}")
        flags = [o for o in options.split(",") if o]
        names: set[str] = set()
        for flag in flags:
            if flag.startswith("size="):
                if _parse_size(flag[5:]) > TMPFS_MAX:
                    raise Denied(f"tmpfs_size:{target}")
                names.add("size")
            elif flag in TMPFS_FLAGS:
                if flag in names:
                    raise Denied(f"tmpfs_dup:{target}")
                names.add(flag)
            else:
                raise Denied(f"tmpfs_option:{target}:{flag}")
        if not {"nosuid", "nodev"} <= names:
            raise Denied(f"tmpfs_nosuid_nodev:{target}")
        if "exec" in names and "noexec" in names:
            raise Denied(f"tmpfs_exec_conflict:{target}")
        if "exec" in names:
            if target != "/tmp" or profile not in TMP_EXEC_PROFILES:  # noqa: S108
                raise Denied(f"tmpfs_exec:{target}")
        elif "noexec" not in names:
            raise Denied(f"tmpfs_noexec_required:{target}")


def _check_mounts(mounts: Any, profile: str) -> None:
    if is_zero(mounts):
        return
    if not isinstance(mounts, list):
        raise Denied("type:HostConfig.Mounts")
    targets: set[str] = set()
    for index, raw in enumerate(mounts):
        path = f"HostConfig.Mounts[{index}]."
        mount = _obj(raw, path)
        _eq(mount, "Type", "volume", path)
        source, target = mount.get("Source"), mount.get("Target")
        if not isinstance(source, str) or VOLUME_RE.fullmatch(source) is None:
            raise Denied(f"mount_source:{source}")
        if not isinstance(target, str) or target not in MOUNT_TARGETS:
            raise Denied(f"mount_target:{target}")
        if target in targets:
            raise Denied(f"mount_target_dup:{target}")
        targets.add(target)
        read_only = mount.get("ReadOnly", False)
        if not isinstance(read_only, bool):
            raise Denied(f"type:{path}ReadOnly")
        options = mount.get("VolumeOptions")
        if not is_zero(options):
            vo = _obj(options, f"{path}VolumeOptions")
            if "Subpath" in vo:
                raise Denied(f"unknown_key:{path}VolumeOptions.Subpath")
            if not isinstance(vo.get("NoCopy", False), bool):
                raise Denied(f"type:{path}VolumeOptions.NoCopy")
            labels = vo.get("Labels")
            if not is_zero(labels) and not all(
                isinstance(v, str) for v in _obj(labels, "VolumeOptions.Labels").values()
            ):
                raise Denied(f"type:{path}VolumeOptions.Labels")
            _check_rest_zero(vo, {"NoCopy", "Labels"}, f"{path}VolumeOptions.")
        _check_rest_zero(mount, {"Type", "Source", "Target", "ReadOnly", "VolumeOptions"}, path)
        kind = source.split("-")[1]
        if target == "/io/input" and not read_only:
            raise Denied("mount_input_rw")
        if kind == "ro" and not read_only and profile != "snapshot":
            raise Denied("mount_ro_rw")
        if profile in {"agent-llm", "catalog", "extract", "code"} and kind in {"task", "ro"}:
            raise Denied(f"mount_{kind}_on_{profile}")
        if profile == "research" and kind == "task":
            raise Denied("mount_task_on_research")
        if target == "/in" and profile != "stage-in":
            raise Denied("mount_in_profile")
        if target == "/snap" and profile != "snapshot":
            raise Denied("mount_snap_profile")


def _check_env(env: Any, profile: str, entry: str) -> None:
    if is_zero(env):
        return
    if not isinstance(env, list):
        raise Denied("type:Env")
    seen: set[str] = set()
    roles: dict[str, str] = {}
    for item in env:
        if not isinstance(item, str) or "=" not in item:
            raise Denied("env_format")
        name, value = item.split("=", 1)
        if ENV_NAME_RE.fullmatch(name) is None or name in seen:
            raise Denied(f"env_name:{name[:64]}")
        seen.add(name)
        if len(value.encode("utf-8")) > ENV_VALUE_MAX:
            raise Denied(f"env_value_too_long:{name}")
        if name == "KIRO_API_KEY":
            if (profile, entry) not in KEY_PAIRS:
                raise Denied(f"env_key_not_allowed:{profile}:{entry}")
        elif name == "GITHUB_READ_TOKEN":
            roles[name] = value  # checked below, once ROLE is known
        elif name not in ENV_FIXED and not name.startswith("OSSBOT_"):
            raise Denied(f"env_name:{name}")
        if name == "ROLE":
            roles["ROLE"] = value
    # GHMCP-2: the read token only for research-profile Kiro jobs of the scout and reviewer roles
    if "GITHUB_READ_TOKEN" in roles and ((profile, entry) != ("research", "kiro")
                                          or roles.get("ROLE") not in GITHUB_READ_ROLES):  # fmt: skip
        raise Denied(f"env_read_token_not_allowed:{profile}:{entry}")


def _check_labels(labels: Any) -> str:
    obj = _obj(labels, "Labels")
    if set(obj) != {"ossbot.sandbox", "ossbot.cid", "ossbot.run", "ossbot.profile"}:
        raise Denied("labels_keys")
    if obj["ossbot.sandbox"] != "1":
        raise Denied("labels_sandbox")
    for key in ("ossbot.cid", "ossbot.run"):
        if not isinstance(obj[key], str) or LABEL_VALUE_RE.fullmatch(obj[key]) is None:
            raise Denied(f"labels_value:{key}")
    profile = obj["ossbot.profile"]
    if profile not in PROFILES:
        raise Denied("labels_profile")
    if profile == "stack":
        raise Denied("profile_stack")
    return str(profile)


def _check_cmd(cmd: Any, profile: str) -> str:
    if not isinstance(cmd, list) or not all(isinstance(c, str) for c in cmd):
        raise Denied("cmd")
    if profile == "stage-in":
        if cmd != STAGE_IN_CMD:
            raise Denied("cmd_stage_in")
        return "true"
    if len(cmd) != 1:
        raise Denied("cmd_args")
    match = ENTRYPOINT_CMD_RE.fullmatch(cmd[0])
    if match is None:
        raise Denied("cmd")
    entry = match.group(1)
    if entry not in CMD_BY_PROFILE.get(profile, frozenset()) and entry != "probe":
        raise Denied(f"cmd_profile:{profile}:{entry}")
    return entry


def _check_image(image: Any, profile: str, policy: FilterPolicy) -> None:
    if not isinstance(image, str):
        raise Denied("image")
    match = IMAGE_RE.fullmatch(image)
    if match is None:
        raise Denied("image")
    name, digest = match.groups()
    if digest not in policy.image_digests.get(name, frozenset()):
        raise Denied("image_digest")
    wanted_llm = profile in LLM_IMAGE_PROFILES
    if (name == "sandbox-llm") is not wanted_llm:
        raise Denied(f"image_profile:{name}:{profile}")


def _check_network(host: Mapping[str, Any], networking: Any, profile: str) -> None:
    mode = host.get("NetworkMode")
    if not isinstance(mode, str) or (mode != "none" and NETWORK_RE.fullmatch(mode) is None):
        raise Denied("network_mode")
    if profile in NETWORK_NONE_PROFILES and mode != "none":
        raise Denied(f"network_mode_must_be_none:{profile}")
    if profile == "catalog" and mode != CATALOG_NETWORK:
        raise Denied("network_mode_catalog")
    if mode == "none":
        if not is_zero(host.get("Dns")) or not is_zero(host.get("DnsSearch")):
            raise Denied("dns_on_none")
    else:
        if host.get("Dns") != ["127.0.0.1"]:
            raise Denied("value:HostConfig.Dns")
        if host.get("DnsSearch") != ["."]:
            raise Denied("value:HostConfig.DnsSearch")
    if is_zero(networking):
        return
    nc = _obj(networking, "NetworkingConfig")
    _check_rest_zero(nc, {"EndpointsConfig"}, "NetworkingConfig.")
    endpoints = nc.get("EndpointsConfig")
    if is_zero(endpoints):
        return
    eps = _obj(endpoints, "NetworkingConfig.EndpointsConfig")
    if list(eps) != [mode]:
        raise Denied("endpoints_network")
    ep = eps[mode]
    if is_zero(ep):
        return
    _check_rest_zero(_obj(ep, "EndpointsConfig.*"), set(), f"NetworkingConfig.EndpointsConfig.{mode}.")


HOSTCONFIG_KNOWN: set[str] = {
    *CREATE_HOSTCONFIG_EMPTY, "Mounts", "Tmpfs", "Privileged", "PublishAllPorts", "OomKillDisable", "AutoRemove",
    "ReadonlyRootfs", "Init", "CapDrop", "SecurityOpt", "NetworkMode", "Dns", "DnsSearch", "PidMode", "IpcMode",
    "UTSMode", "UsernsMode", "CgroupnsMode", "CgroupParent", "Runtime", "PidsLimit", "Memory", "MemorySwap",
    "NanoCpus", "Ulimits", "ShmSize", "LogConfig", "RestartPolicy", "Isolation", "ConsoleSize", "MemorySwappiness",
}  # fmt: skip
CONFIG_KNOWN: set[str] = {
    "Image", "User", "Hostname", "Labels", "Entrypoint", "Cmd", "WorkingDir", "Env", "Tty", "OpenStdin",
    "AttachStdin", "StdinOnce", "NetworkDisabled", "ExposedPorts", "Volumes", "Healthcheck", "OnBuild", "Shell",
    "StopSignal", "HostConfig", "NetworkingConfig", "AttachStdout", "AttachStderr",
}  # fmt: skip


def _check_hostconfig(host: dict[str, Any], profile: str) -> None:
    p = "HostConfig."
    for key in CREATE_HOSTCONFIG_EMPTY:
        if not is_zero(host.get(key)):
            raise Denied(f"value:{p}{key}")
    _check_mounts(host.get("Mounts"), profile)
    _check_tmpfs(host.get("Tmpfs"), profile)
    for key in ("Privileged", "PublishAllPorts", "OomKillDisable", "AutoRemove"):
        if host.get(key) not in (None, False):
            raise Denied(f"value:{p}{key}")
    for key in ("ReadonlyRootfs", "Init"):
        _eq(host, key, True, p)
    _eq(host, "CapDrop", ["ALL"], p)
    _eq(host, "SecurityOpt", ["no-new-privileges"], p)
    for key in ("PidMode", "UTSMode", "UsernsMode", "CgroupnsMode"):
        _zero_or(host, key, {"private"}, p)
    _zero_or(host, "IpcMode", {"private", "none"}, p)
    if not is_zero(host.get("CgroupParent")):
        raise Denied(f"value:{p}CgroupParent")
    _zero_or(host, "Runtime", {"runc"}, p)
    _int_in(host, "PidsLimit", 1, 4096, p)
    memory = _int_in(host, "Memory", MEMORY_MIN, MEMORY_MAX, p)
    if host.get("MemorySwap") != memory:
        raise Denied(f"value:{p}MemorySwap")
    _int_in(host, "NanoCpus", NANOCPUS_MIN, NANOCPUS_MAX, p)
    _eq(host, "Ulimits", ULIMITS, p)
    shm = host.get("ShmSize")
    if not is_zero(shm):
        _int_in(host, "ShmSize", 1, SHM_MAX, p)
    log_config = host.get("LogConfig")
    if not is_zero(log_config):
        lc = _obj(log_config, f"{p}LogConfig")
        _zero_or(lc, "Type", {"local"}, f"{p}LogConfig.")
        _check_rest_zero(lc, {"Type"}, f"{p}LogConfig.")
    restart = host.get("RestartPolicy")
    if not is_zero(restart):
        rp = _obj(restart, f"{p}RestartPolicy")
        _zero_or(rp, "Name", {"no"}, f"{p}RestartPolicy.")
        _check_rest_zero(rp, {"Name"}, f"{p}RestartPolicy.")
    if not is_zero(host.get("Isolation")):
        raise Denied(f"value:{p}Isolation")
    console = host.get("ConsoleSize")
    if not is_zero(console) and console != [0, 0]:
        raise Denied(f"value:{p}ConsoleSize")
    # The pinned CLI always sends -1 (= kernel default) for an unset --memory-swappiness (§23 item 15).
    if host.get("MemorySwappiness") not in (None, -1):
        raise Denied(f"value:{p}MemorySwappiness")
    _check_rest_zero(host, HOSTCONFIG_KNOWN, p)


@dataclass(frozen=True)
class CreateFacts:
    name: str
    profile: str
    entrypoint: str
    network: str


def validate_create(req: Request, policy: FilterPolicy) -> CreateFacts:
    """The complete create specification (§10.8). Raises ``Denied`` with the first violation."""
    _check_query(req, {"name", "platform"})
    name = _q(req, "name")
    if name is None or CONTAINER_NAME_RE.fullmatch(name) is None:
        raise Denied("container_name")
    if not is_zero(_q(req, "platform")):
        raise Denied("query:platform")
    body = _obj(parse_json(req.body), "<body>")
    profile = _check_labels(body.get("Labels"))
    entry = _check_cmd(body.get("Cmd"), profile)
    _check_image(body.get("Image"), profile, policy)
    _eq(body, "User", "10001:10001", "")
    _eq(body, "Hostname", "sbx", "")
    if body.get("Entrypoint") is not None:
        raise Denied("value:Entrypoint")
    if body.get("WorkingDir") not in WORKDIRS:
        raise Denied("value:WorkingDir")
    _check_env(body.get("Env"), profile, entry)
    for key in ("Tty", "OpenStdin", "AttachStdin", "StdinOnce", "NetworkDisabled"):
        if body.get(key) not in (None, False):
            raise Denied(f"value:{key}")
    # `docker create` sets these for a later attach; harmless because /attach is never allowed.
    for key in ("AttachStdout", "AttachStderr"):
        if not isinstance(body.get(key, False), bool):
            raise Denied(f"type:{key}")
    for key in ("ExposedPorts", "Volumes", "Healthcheck", "OnBuild", "Shell"):
        if not is_zero(body.get(key)):
            raise Denied(f"value:{key}")
    _zero_or(body, "StopSignal", {"SIGTERM"}, "")
    host = _obj(body.get("HostConfig"), "HostConfig")
    _check_hostconfig(host, profile)
    _check_network(host, body.get("NetworkingConfig"), profile)
    _check_rest_zero(body, CONFIG_KNOWN, "")
    return CreateFacts(name=name, profile=profile, entrypoint=entry, network=str(host["NetworkMode"]))


# --------------------------------------------------------------------- other bodies
def validate_network_create(req: Request) -> str:
    _check_query(req, set())
    body = _obj(parse_json(req.body), "<body>")
    name = body.get("Name")
    if not isinstance(name, str) or NETWORK_RE.fullmatch(name) is None:
        raise Denied("network_name")
    _eq(body, "Internal", True, "")
    _zero_or(body, "Driver", {"bridge"}, "")
    if body.get("EnableIPv4") not in (None, True):
        raise Denied("value:EnableIPv4")
    if not is_zero(body.get("Options")):
        raise Denied("value:Options")
    labels = _obj(body.get("Labels"), "Labels")
    if labels.get("ossbot.sandbox") != "1":
        raise Denied("labels_sandbox")
    for key, value in labels.items():
        if key not in {"ossbot.sandbox", "ossbot.cid", "ossbot.profile"} or not isinstance(value, str):
            raise Denied(f"labels_key:{key}")
        if key != "ossbot.sandbox" and LABEL_VALUE_RE.fullmatch(value) is None:
            raise Denied(f"labels_value:{key}")
    ipam = _obj(body.get("IPAM"), "IPAM")
    _zero_or(ipam, "Driver", {"default"}, "IPAM.")
    if not is_zero(ipam.get("Options")):
        raise Denied("value:IPAM.Options")
    configs = ipam.get("Config")
    if not isinstance(configs, list) or len(configs) != 1:
        raise Denied("ipam_config")
    config = _obj(configs[0], "IPAM.Config[0]")
    subnet = config.get("Subnet")
    try:
        net = ipaddress.ip_network(subnet if isinstance(subnet, str) else "", strict=True)
    except ValueError as exc:
        raise Denied("ipam_subnet") from exc
    if not isinstance(net, ipaddress.IPv4Network) or net.prefixlen != 28 or not net.subnet_of(TASK_POOL):
        raise Denied("ipam_subnet")
    _check_rest_zero(config, {"Subnet"}, "IPAM.Config[0].")
    _check_rest_zero(ipam, {"Driver", "Config", "Options"}, "IPAM.")
    _check_rest_zero(
        body, {"Name", "Internal", "Driver", "EnableIPv4", "Options", "Labels", "IPAM", "CheckDuplicate"}, ""
    )
    return name


def validate_volume_create(req: Request) -> str:
    _check_query(req, set())
    body = _obj(parse_json(req.body), "<body>")
    name = body.get("Name")
    if not isinstance(name, str) or VOLUME_RE.fullmatch(name) is None:
        raise Denied("volume_name")
    _zero_or(body, "Driver", {"local"}, "")
    if not is_zero(body.get("DriverOpts")):
        raise Denied("value:DriverOpts")
    labels = body.get("Labels")
    if not is_zero(labels):
        for key, value in _obj(labels, "Labels").items():
            if not key.startswith("ossbot.") or not isinstance(value, str) or len(value) > 64:
                raise Denied(f"labels_key:{key}")
    _check_rest_zero(body, {"Name", "Driver", "DriverOpts", "Labels"}, "")
    return name


def _label_filter_ok(raw: str | None) -> bool:
    if raw is None:
        return False
    try:
        filters = parse_json(raw.encode("utf-8"))
    except Denied:
        return False
    if not isinstance(filters, dict):
        return False
    label = filters.get("label")
    if isinstance(label, dict):
        return label.get("ossbot.sandbox=1") is True
    if isinstance(label, list):
        return "ossbot.sandbox=1" in label
    return False


# ----------------------------------------------------------------------- dispatcher
_ROUTES: list[tuple[str, re.Pattern[str], str]] = [
    ("GET", re.compile(r"^/_ping$"), "ping"),
    ("HEAD", re.compile(r"^/_ping$"), "ping"),
    ("GET", re.compile(r"^/version$"), "version"),
    ("GET", re.compile(r"^/info$"), "info"),
    ("POST", re.compile(r"^/containers/create$"), "containers/create"),
    ("GET", re.compile(r"^/containers/json$"), "containers/list"),
    ("POST", re.compile(r"^/containers/(?P<id>[^/]+)/start$"), "containers/start"),
    ("POST", re.compile(r"^/containers/(?P<id>[^/]+)/wait$"), "containers/wait"),
    ("POST", re.compile(r"^/containers/(?P<id>[^/]+)/kill$"), "containers/kill"),
    ("DELETE", re.compile(r"^/containers/(?P<id>[^/]+)$"), "containers/delete"),
    ("GET", re.compile(r"^/containers/(?P<id>[^/]+)/json$"), "containers/inspect"),
    ("GET", re.compile(r"^/containers/(?P<id>[^/]+)/logs$"), "containers/logs"),
    ("PUT", re.compile(r"^/containers/(?P<id>[^/]+)/archive$"), "containers/archive-put"),
    ("HEAD", re.compile(r"^/containers/(?P<id>[^/]+)/archive$"), "containers/archive-head"),
    ("GET", re.compile(r"^/containers/(?P<id>[^/]+)/archive$"), "containers/archive-get"),
    ("POST", re.compile(r"^/networks/create$"), "networks/create"),
    ("GET", re.compile(r"^/networks/(?P<id>[^/]+)$"), "networks/inspect"),
    ("POST", re.compile(r"^/networks/(?P<id>[^/]+)/connect$"), "networks/connect"),
    ("POST", re.compile(r"^/networks/(?P<id>[^/]+)/disconnect$"), "networks/disconnect"),
    ("DELETE", re.compile(r"^/networks/(?P<id>[^/]+)$"), "networks/delete"),
    ("POST", re.compile(r"^/volumes/create$"), "volumes/create"),
    ("DELETE", re.compile(r"^/volumes/(?P<id>[^/]+)$"), "volumes/delete"),
    ("GET", re.compile(r"^/system/df$"), "system/df"),
]


def strip_version(path: str) -> str:
    return API_VERSION_RE.sub("", path, count=1)


def _sandbox_container(lookup: Lookup, ref: str) -> ContainerFacts:
    if OBJECT_ID_RE.fullmatch(ref) is None:
        raise Denied("container_ref")
    facts = lookup.container(ref)
    if facts is None or not facts.sandbox:
        raise Denied("container_not_sandbox")
    return facts


def _ossbot_network(lookup: Lookup, ref: str) -> NetworkFacts:
    if OBJECT_ID_RE.fullmatch(ref) is None:
        raise Denied("network_ref")
    facts = lookup.network(ref)
    if facts is None or NETWORK_RE.fullmatch(facts.name) is None:
        raise Denied("network_not_ossbot")
    return facts


def _check_connect(req: Request, lookup: Lookup, connect: bool) -> None:
    _check_query(req, set())
    body = _obj(parse_json(req.body), "<body>")
    target = body.get("Container")
    if not isinstance(target, str) or OBJECT_ID_RE.fullmatch(target) is None:
        raise Denied("connect_container")
    facts = lookup.container(target)
    if facts is None:
        raise Denied("connect_container")
    is_proxy = facts.name == PROXY_CONTAINER
    if not (facts.sandbox or is_proxy):
        raise Denied("connect_container")
    if connect:
        endpoint = body.get("EndpointConfig")
        if not is_zero(endpoint):
            ep = _obj(endpoint, "EndpointConfig")
            aliases = ep.get("Aliases")
            if not is_zero(aliases) and not (is_proxy and aliases == ["proxy"]):
                raise Denied("connect_aliases")
            _check_rest_zero(ep, {"Aliases"}, "EndpointConfig.")
        _check_rest_zero(body, {"Container", "EndpointConfig"}, "")
    else:
        if body.get("Force") not in (None, False, True):
            raise Denied("value:Force")
        _check_rest_zero(body, {"Container", "Force"}, "")


def _route(req: Request, lookup: Lookup, policy: FilterPolicy, op: str, ref: str) -> None:
    if op in {"ping", "version", "info"}:
        return
    if op == "containers/create":
        _flags(policy, "docker_filter:containers/create")
        validate_create(req, policy)
    elif op == "containers/list":
        _check_query(req, {"all", "filters", "limit", "size"})
        if not _label_filter_ok(_q(req, "filters")):
            raise Denied("list_filter")
    elif op == "containers/start":
        _check_query(req, set())
        if req.body.strip() not in (b"", b"{}", b"null"):
            raise Denied("start_body")
        _flags(policy, "docker_filter:containers/start")
        if _sandbox_container(lookup, ref).profile == "stage-in":
            raise Denied("start_stage_in")
    elif op == "containers/wait":
        _check_query(req, {"condition"})
        condition = _q(req, "condition")
        if condition is not None and condition not in WAIT_CONDITIONS:
            raise Denied("query:condition")
        _sandbox_container(lookup, ref)
    elif op == "containers/kill":
        _check_query(req, {"signal"})
        signal = _q(req, "signal")
        if signal is not None and signal not in KILL_SIGNALS:
            raise Denied("query:signal")
        _sandbox_container(lookup, ref)
    elif op == "containers/delete":
        _check_query(req, {"v", "force", "link"})
        _bool_query(req, "link", required=False)
        _bool_query(req, "v")
        _bool_query(req, "force")
        _sandbox_container(lookup, ref)
    elif op == "containers/inspect":
        _check_query(req, {"size"})
        _sandbox_container(lookup, ref)
    elif op == "containers/logs":
        _check_query(req, {"stdout", "stderr", "tail", "timestamps", "since", "until", "follow", "details"})
        _sandbox_container(lookup, ref)
    elif op in {"containers/archive-put", "containers/archive-head", "containers/archive-get"}:
        _check_archive(req, lookup, op, ref)
    elif op == "networks/create":
        validate_network_create(req)
    elif op in {"networks/connect", "networks/disconnect"}:
        _ossbot_network(lookup, ref)
        _check_connect(req, lookup, op == "networks/connect")
    elif op in {"networks/delete", "networks/inspect"}:
        # `docker network rm` inspects the network by name first (recorded from the pinned CLI).
        _check_query(req, {"verbose", "scope"} if op == "networks/inspect" else set())
        _ossbot_network(lookup, ref)
    elif op == "volumes/create":
        validate_volume_create(req)
    elif op == "volumes/delete":
        _check_query(req, {"force"})
        _bool_query(req, "force")
        if VOLUME_RE.fullmatch(ref) is None:
            raise Denied("volume_name")
    elif op == "system/df":
        _check_query(req, {"type"})
    else:  # pragma: no cover - every route above is handled
        raise Denied("route")


def _check_archive(req: Request, lookup: Lookup, op: str, ref: str) -> None:
    path = _q(req, "path")
    if op == "containers/archive-get" or (op == "containers/archive-head" and path == "/io/output"):
        _check_query(req, {"path"})
        if path != "/io/output":
            raise Denied("archive_path")
        facts = _sandbox_container(lookup, ref)
        if facts.profile == "stage-in" or facts.status not in {"exited", "created"}:
            raise Denied("archive_get_state")
        return
    if op == "containers/archive-put":
        _check_query(req, {"path", "noOverwriteDirNonDir", "copyUIDGID"})
        _bool_query(req, "noOverwriteDirNonDir", required=True)
        _bool_query(req, "copyUIDGID", required=False)
    else:
        _check_query(req, {"path"})
    if path != "/in":
        raise Denied("archive_path")
    facts = _sandbox_container(lookup, ref)
    if facts.profile != "stage-in" or facts.status != "created":
        raise Denied("archive_put_target")


def decide(req: Request, lookup: Lookup, policy: FilterPolicy) -> Decision:
    path = strip_version(req.path)
    for method, pattern, op in _ROUTES:
        if req.method != method:
            continue
        match = pattern.fullmatch(path)
        if match is None:
            continue
        ref = match.groupdict().get("id") or ""
        try:
            _route(req, lookup, policy, op, ref)
        except Denied as exc:
            return Decision(False, exc.reason, op)
        return Decision(True, "ok", op)
    return Decision(False, "route_not_allowed", f"{req.method} {path}")


def parse_query(raw: str) -> dict[str, list[str]]:
    """``urllib.parse.parse_qs`` with blank values kept and strict parsing."""
    try:
        return parse_qs(raw, keep_blank_values=True, strict_parsing=bool(raw), max_num_fields=32)
    except ValueError as exc:
        raise Denied("bad_query") from exc


def load_image_allowlist(lock_path: Path) -> dict[str, frozenset[str]]:
    """Digest allowlist from the root-owned versions.lock.yaml copy (``ossbot.common.images``).

    Missing or ``pending`` entries allow nothing for that image (fail closed).
    """
    return {name: frozenset({digest}) for name, digest in load_image_pins(lock_path).digests.items()}
