"""Fixed docker argv templates (DESIGN §10.2 run template, §10.4 split coder, M2, H1).

Caller strings never become flags: every value here comes from a validated ``JobSpec`` (typed
args), the fixed templates in ``profiles`` and sandboxd's own names. ``build_argv`` re-checks
the result before returning it, and ``tests/unit/sandboxd/test_argv.py`` feeds every variant
through ``ossbot.dockerfilter.rules`` (M4); this module deliberately does not import the filter,
so the two layers stay independent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ossbot.sandboxd.profiles import AGENT_LLM_CPUS, AGENT_LLM_MEMORY_MB, TEMPLATES, cid_slug
from ossbot.sandboxd.runtime.base import Mount, RunSpec

ENTRYPOINTS_DIR = "/opt/ossbot/entrypoints"
PROXY_URL = "http://proxy:3128"
TMP_EXEC = "/tmp:rw,exec,nosuid,nodev,size=2g"  # noqa: S108 - container path
TMP_NOEXEC = "/tmp:rw,noexec,nosuid,nodev,size=2g"  # noqa: S108
# mode=1777: Docker mounts the tmpfs root as root:root 0700 (verified live on Docker 29.9.1, FEAT-004),
# so uid 10001 could not write its own HOME without it.
HOME_TMPFS = "/home/agent:rw,noexec,nosuid,nodev,size=512m,mode=1777"
LLM_HOME = "/home/agent"
LLM_CWD = "/home/agent/cwd"
RW_HOME = "/work/.home"
MOUNT_TARGETS = frozenset({"/work", "/work/repo", "/io/input", "/io/output", "/run/exec", "/in", "/snap"})
ENV_NAMES = frozenset({"ROLE", "PROMPT_FILE", "MODEL", "EFFORT", "TRUST_TOOLS", "REQUIRE_MCP"})
KEY_PAIRS = frozenset({("research", "kiro"), ("agent-llm", "kiro"), ("catalog", "list_models")})
_NAME = re.compile(r"^ossbot-[a-z0-9_-]{1,80}$")
_LABEL = re.compile(r"^[a-z0-9_-]{1,40}$")
_VOLUME = re.compile(r"^ossbot-(task|out|in|exec|ro)-[a-z0-9_-]+$")
_NETWORK = re.compile(r"^(none|ossbot-t-[a-z0-9-]+)$")
_IMAGE = re.compile(r"^ossbot/(sandbox-full|sandbox-llm)@sha256:[0-9a-f]{64}$")
_ENV_NAME = re.compile(r"^(OSSBOT_[A-Z0-9_]{1,40}|[A-Z_]{1,20})$")
_ENTRY = re.compile(r"^[a-z_]{1,32}$")


class ArgvError(ValueError):
    pass


@dataclass(frozen=True)
class Volumes:
    run_id: str
    cid: str

    @property
    def input(self) -> str:
        return f"ossbot-in-{self.run_id}"

    @property
    def output(self) -> str:
        return f"ossbot-out-{self.run_id}"

    @property
    def exec_output(self) -> str:
        return f"ossbot-out-{self.run_id}-exec"

    @property
    def ro(self) -> str:
        return f"ossbot-ro-{self.run_id}"

    @property
    def exec(self) -> str:
        return f"ossbot-exec-{self.run_id}"

    @property
    def task(self) -> str:
        return f"ossbot-task-{cid_slug(self.cid)}"


def job_spec(
    template_name: str,
    *,
    run_id: str,
    cid: str,
    entrypoint: str,
    image_ref: str,
    network: str,
    repo_mode: str,
    env: dict[str, str] | None = None,
    cpus: float = 2.0,
    memory_mb: int = 4096,
) -> RunSpec:
    """A job container (fetch, validate, package, research, agent-llm, agent-exec, catalog, probe)."""
    t = TEMPLATES[template_name]
    vols = Volumes(run_id, cid)
    out = vols.exec_output if template_name == "agent-exec" else vols.output
    mounts = [Mount(vols.input, "/io/input", read_only=True), Mount(out, "/io/output")]
    home: str | None = None
    workdir = "/"
    if template_name == "agent-llm" or repo_mode == "none":
        pass
    elif repo_mode == "rw":
        mounts.append(Mount(vols.task, "/work"))
        home, workdir = RW_HOME, "/work/repo"
    elif repo_mode == "snapshot":
        mounts.append(Mount(vols.ro, "/work/repo", read_only=True))
    else:
        raise ArgvError(f"repo_mode {repo_mode}")
    if t.llm_home:
        home, workdir = LLM_HOME, LLM_CWD
    if t.exec_volume:
        mounts.append(Mount(vols.exec, "/run/exec"))
    if template_name == "agent-llm":
        cpus, memory_mb = AGENT_LLM_CPUS, AGENT_LLM_MEMORY_MB
    name = f"ossbot-{run_id}-exec" if template_name == "agent-exec" else f"ossbot-{run_id}"
    return RunSpec(
        name=name, run_id=run_id, cid=cid_slug(cid), profile=t.profile, entrypoint=entrypoint, image_ref=image_ref,
        network=network, mounts=tuple(mounts), env=tuple(sorted((env or {}).items())),
        key=t.key and (t.profile, entrypoint) in KEY_PAIRS, tmp_exec=t.tmp_exec, llm_home=t.llm_home,
        workdir=workdir, home=home, cpus=cpus, memory_mb=memory_mb,
    )  # fmt: skip


def stagein_spec(run_id: str, cid: str, image_ref: str) -> RunSpec:
    """Created, never started; the daemon PUTs the input tar to ``/in`` (M3)."""
    return RunSpec(
        name=f"ossbot-{run_id}-stagein", run_id=run_id, cid=cid_slug(cid), profile="stage-in", entrypoint="true",
        image_ref=image_ref, network="none", mounts=(Mount(Volumes(run_id, cid).input, "/in"),), create_only=True,
    )  # fmt: skip


def snapshot_spec(run_id: str, cid: str, image_ref: str) -> RunSpec:
    """Task volume read-only at /work, the per-run ro volume rw at /snap (H1, N3)."""
    vols = Volumes(run_id, cid)
    return RunSpec(
        name=f"ossbot-{run_id}-snapshot", run_id=run_id, cid=cid_slug(cid), profile="snapshot",
        entrypoint="snapshot", image_ref=image_ref, network="none",
        mounts=(Mount(vols.task, "/work", read_only=True), Mount(vols.ro, "/snap")), cpus=1.0, memory_mb=1024,
    )  # fmt: skip


def probe_spec(
    run_id: str, cid: str, image_ref: str, mode: str, mounts: tuple[Mount, ...], suffix: str = "probe"
) -> RunSpec:
    """Internal ``probe.sh`` helper (snapshot post-check, fixture extraction); never gets the key."""
    if re.fullmatch(r"[a-z_]{1,32}", mode) is None:
        raise ArgvError("probe mode")
    return RunSpec(
        name=f"ossbot-{run_id}-{suffix}", run_id=run_id, cid=cid_slug(cid), profile="probe", entrypoint="probe",
        image_ref=image_ref, network="none", mounts=mounts, env=(("OSSBOT_PROBE", mode),), cpus=1.0, memory_mb=256,
    )  # fmt: skip


def _check(spec: RunSpec) -> None:
    if _NAME.fullmatch(spec.name) is None:
        raise ArgvError("name")
    for value in (spec.cid, spec.run_id, spec.profile):
        if _LABEL.fullmatch(value) is None:
            raise ArgvError("label")
    if _IMAGE.fullmatch(spec.image_ref) is None:
        raise ArgvError("image")
    if _NETWORK.fullmatch(spec.network) is None:
        raise ArgvError("network")
    if spec.entrypoint != "true" and _ENTRY.fullmatch(spec.entrypoint) is None:
        raise ArgvError("entrypoint")
    if (spec.entrypoint == "true") != (spec.profile == "stage-in"):
        raise ArgvError("stage-in command")
    targets = [m.target for m in spec.mounts]
    if len(targets) != len(set(targets)):
        raise ArgvError("duplicate mount target")
    for mount in spec.mounts:
        if _VOLUME.fullmatch(mount.source) is None or mount.target not in MOUNT_TARGETS:
            raise ArgvError("mount")
    for key, value in spec.env:
        if _ENV_NAME.fullmatch(key) is None or not (key in ENV_NAMES or key.startswith("OSSBOT_")):
            raise ArgvError(f"env {key}")
        if "\x00" in value or "\n" in value or len(value.encode()) > 4096:
            raise ArgvError(f"env value {key}")
    if spec.key and (spec.profile, spec.entrypoint) not in KEY_PAIRS:
        raise ArgvError("key")
    if not 0 < spec.cpus <= 8 or not 64 <= spec.memory_mb <= 6144:
        raise ArgvError("limits")


def build_argv(spec: RunSpec, docker: str = "docker") -> list[str]:
    _check(spec)
    cmd = "/bin/true" if spec.profile == "stage-in" else f"{ENTRYPOINTS_DIR}/{spec.entrypoint}.sh"
    argv = [docker, "create"] if spec.create_only else [docker, "run", "--detach"]
    argv += [
        "--init", "--pull", "never", "--name", spec.name, "--hostname", "sbx",
        "--label", "ossbot.sandbox=1", "--label", f"ossbot.cid={spec.cid}", "--label", f"ossbot.run={spec.run_id}",
        "--label", f"ossbot.profile={spec.profile}",
        "--user", "10001:10001", "--read-only",
        "--tmpfs", TMP_EXEC if spec.tmp_exec else TMP_NOEXEC,
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "4096", "--memory", f"{spec.memory_mb}m", "--memory-swap", f"{spec.memory_mb}m",
        "--cpus", f"{spec.cpus:g}", "--ulimit", "nofile=65536:65536",
        "--network", spec.network,
    ]  # fmt: skip
    if spec.network != "none":
        argv += ["--dns", "127.0.0.1", "--dns-search", "."]
    for mount in spec.mounts:
        argv += [
            "--mount",
            f"type=volume,src={mount.source},dst={mount.target}" + (",readonly" if mount.read_only else ""),
        ]
    if spec.llm_home:
        argv += ["--tmpfs", HOME_TMPFS]
    if spec.home is not None:
        argv += ["--env", f"HOME={spec.home}"]
    argv += ["--workdir", spec.workdir]
    argv += ["--env", f"HTTPS_PROXY={PROXY_URL}", "--env", f"HTTP_PROXY={PROXY_URL}", "--env", "NO_PROXY="]
    if spec.key:
        argv += ["--env", "KIRO_API_KEY"]  # name-only: the value never appears in argv (ps)
    for key, value in spec.env:
        argv += ["--env", f"{key}={value}"]
    argv += [spec.image_ref, cmd]
    return argv
