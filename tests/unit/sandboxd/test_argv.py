"""sandboxd argv templates (DESIGN §10.2 assertions list, §22.1, M2, M4, M11, H1)."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

import pytest

from ossbot.dockerfilter import rules
from ossbot.sandboxd.argv import MOUNT_TARGETS, ArgvError, build_argv, job_spec
from ossbot.sandboxd.profiles import JobSpec, SpecError, validate_job
from ossbot.sandboxd.runtime.base import RunSpec
from ossbot.sandboxd.subnets import SubnetAllocator
from tests.sandboxharness import FULL, FakeLookup, create_request, policy, variants
from tests.unit.sandboxd.conftest import Sbx

FORBIDDEN_FLAGS = (
    "-v",
    "--volume",
    "--privileged",
    "--cap-add",
    "--device",
    "--pid",
    "--ipc",
    "--userns",
    "--sysctl",
    "--add-host",
    "-p",
    "--publish",
    "--publish-all",
    "--entrypoint",
    "--volumes-from",
)
JOB_VARIANTS = [n for n in variants() if n not in {"stage-in", "snapshot", "snapcheck", "fixture"}]


def _pairs(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == flag]


def _mounts(argv: list[str]) -> list[dict[str, str]]:
    out = []
    for spec in _pairs(argv, "--mount"):
        fields = dict(p.split("=", 1) for p in spec.split(",") if "=" in p)
        fields["readonly"] = "1" if "readonly" in spec.split(",") else "0"
        out.append(fields)
    return out


@pytest.mark.parametrize("name", sorted(variants()))
def test_forbidden_flags_never_present(name: str) -> None:
    argv = build_argv(variants()[name])
    for flag in FORBIDDEN_FLAGS:
        assert flag not in argv, flag
    assert _pairs(argv, "--network")[0] in {"none"} or _pairs(argv, "--network")[0].startswith("ossbot-t-")
    assert _pairs(argv, "--security-opt") == ["no-new-privileges"]
    assert _pairs(argv, "--cap-drop") == ["ALL"]
    assert "--read-only" in argv and "--init" in argv
    assert _pairs(argv, "--user") == ["10001:10001"]
    assert all(m["type"] == "volume" for m in _mounts(argv))


@pytest.mark.parametrize("name", sorted(variants()))
def test_dns_only_and_always_on_networked(name: str) -> None:
    argv = build_argv(variants()[name])
    networked = _pairs(argv, "--network")[0] != "none"
    assert (_pairs(argv, "--dns") == ["127.0.0.1"]) is networked
    assert (_pairs(argv, "--dns-search") == ["."]) is networked


@pytest.mark.parametrize("name", JOB_VARIANTS)
def test_io_mounts_present_with_input_read_only(name: str) -> None:
    mounts = {m["dst"]: m for m in _mounts(build_argv(variants()[name]))}
    assert mounts["/io/input"]["readonly"] == "1"
    assert mounts["/io/output"]["readonly"] == "0"


@pytest.mark.parametrize("name", sorted(variants()))
def test_every_mount_target_is_a_fixed_image_directory(name: str) -> None:
    for mount in _mounts(build_argv(variants()[name])):
        assert mount["dst"] in MOUNT_TARGETS


@pytest.mark.parametrize("name", sorted(variants()))
def test_key_only_for_research_agent_llm_catalog(name: str) -> None:
    spec = variants()[name]
    has_key = "KIRO_API_KEY" in _pairs(build_argv(spec), "--env")
    assert has_key is (
        (spec.profile, spec.entrypoint) in {("research", "kiro"), ("agent-llm", "kiro"), ("catalog", "list_models")}
    )
    # the value never appears in argv (name-only --env form)
    assert not any(v.startswith("KIRO_API_KEY=") for v in _pairs(build_argv(spec), "--env"))


@pytest.mark.parametrize("name", sorted(variants()))
def test_read_token_only_for_research_scout_and_reviewer(name: str) -> None:
    """GHMCP-2: name-only GITHUB_READ_TOKEN for the MCP roles; never fetch/validate or code-running jobs."""
    spec = variants()[name]
    env = _pairs(build_argv(spec), "--env")
    role = dict(spec.env).get("ROLE")
    expected = (spec.profile, spec.entrypoint) == ("research", "kiro") and role in {"scout", "reviewer"}
    assert ("GITHUB_READ_TOKEN" in env) is expected, name
    assert not any(v.startswith("GITHUB_READ_TOKEN=") for v in env)


@pytest.mark.parametrize("role", ["scout", "reviewer", "planner", "vetter", "summarizer"])
def test_job_spec_sets_the_read_token_by_role(role: str) -> None:
    from tests.sandboxharness import CID, LLM, NET, RUN

    spec = job_spec("research", run_id=RUN, cid=CID, entrypoint="kiro", image_ref=LLM, network=NET,
                    repo_mode="snapshot", env={"ROLE": role})  # fmt: skip
    assert spec.github_read is (role in {"scout", "reviewer"})


def test_a_forged_read_token_spec_is_refused() -> None:
    import dataclasses

    spec = dataclasses.replace(variants()["agent-llm"], github_read=True)
    with pytest.raises(ArgvError, match="github_read"):
        build_argv(spec)


@pytest.mark.parametrize("name", sorted(variants()))
def test_tmp_exec_only_for_code_running_profiles(name: str) -> None:
    spec = variants()[name]
    tmpfs = dict(t.split(":", 1) for t in _pairs(build_argv(spec), "--tmpfs"))
    exec_on_tmp = "exec" in tmpfs["/tmp"].split(",")
    assert exec_on_tmp is (spec.profile in {"fetch", "validate", "agent-exec", "package", "code"})
    if "/home/agent" in tmpfs:
        assert "noexec" in tmpfs["/home/agent"].split(",")


def test_agent_llm_has_no_task_or_snapshot_mount() -> None:
    sources = [m["src"] for m in _mounts(build_argv(variants()["agent-llm"]))]
    assert not [s for s in sources if s.startswith(("ossbot-task-", "ossbot-ro-"))]
    assert "ossbot-exec-r1-plan" in sources


def test_research_mounts_snapshot_read_only_and_never_task() -> None:
    mounts = _mounts(build_argv(variants()["research-snapshot"]))
    ro = [m for m in mounts if m["src"].startswith("ossbot-ro-")]
    assert ro == [{"type": "volume", "src": "ossbot-ro-r1-plan", "dst": "/work/repo", "readonly": "1"}]
    assert not [m for m in mounts if m["src"].startswith("ossbot-task-")]


def test_catalog_shape() -> None:
    argv = build_argv(variants()["catalog"])
    sources = [m["src"] for m in _mounts(argv)]
    assert not [s for s in sources if s.startswith(("ossbot-task-", "ossbot-ro-"))]
    assert _pairs(argv, "--network") == ["ossbot-t-sys-catalog-catalog"]
    assert argv[-1] == "/opt/ossbot/entrypoints/list_models.sh"


@pytest.mark.parametrize("name", ["research-snapshot", "research-none", "agent-llm", "catalog"])
def test_llm_containers_use_tmpfs_home_and_cwd(name: str) -> None:
    argv = build_argv(variants()[name])
    assert "HOME=/home/agent" in _pairs(argv, "--env")
    assert _pairs(argv, "--workdir") == ["/home/agent/cwd"]
    assert any(t.startswith("/home/agent:") for t in _pairs(argv, "--tmpfs"))


def test_rw_profiles_use_task_home() -> None:
    argv = build_argv(variants()["fetch"])
    assert "HOME=/work/.home" in _pairs(argv, "--env")
    assert _pairs(argv, "--workdir") == ["/work/repo"]


@pytest.mark.parametrize("name", sorted(variants()))
def test_filter_accepts_every_variant(name: str, tmp_path: Path) -> None:
    decision = rules.decide(create_request(variants()[name]), FakeLookup(), policy(tmp_path))
    assert decision.allow, decision.reason


def test_command_is_fixed_entrypoint() -> None:
    for name, spec in variants().items():
        argv = build_argv(spec)
        assert argv[-2].startswith("ossbot/sandbox-"), name
        expected = "/bin/true" if spec.profile == "stage-in" else f"/opt/ossbot/entrypoints/{spec.entrypoint}.sh"
        assert argv[-1] == expected


@pytest.mark.parametrize(
    "change",
    [
        {"network": "host"},
        {"image_ref": "alpine:latest"},
        {"name": "evil"},
        {"env": (("LD_PRELOAD", "/x"),)},
        {"env": (("OSSBOT_X", "a\nb"),)},
        {"key": True},
        {"memory_mb": 99999},
        {"profile": "Fetch"},
    ],
)
def test_build_argv_rechecks_every_value(change: dict[str, Any]) -> None:
    base = variants()["fetch"]
    spec = RunSpec(**{**base.__dict__, **change})
    with pytest.raises(ArgvError):
        build_argv(spec)


def test_job_spec_never_takes_flags_from_values() -> None:
    spec = job_spec(
        "fetch",
        run_id="r1",
        cid="c_01ABCDEFGH",
        entrypoint="fetch",
        image_ref=FULL,
        network="ossbot-t-c-01abcdefgh-fetch",
        repo_mode="rw",
        env={"OSSBOT_ARG_REPO": "--privileged"},
    )
    argv = build_argv(spec)
    assert "--privileged" not in argv  # only ever inside a single KEY=value argument
    assert "OSSBOT_ARG_REPO=--privileged" in argv


def test_scout_fetch_env_only_when_prefetch_is_off() -> None:
    """CR-004 scout clone: OSSBOT_ARG_PREFETCH=0 only for prefetch false; the default env is unchanged."""
    from ossbot.sandboxd.jobs import _env_for
    from ossbot.sandboxd.profiles import validate_job

    base = {"run_id": "r1", "cid": "c_01ABCDEFGH", "profile": "fetch", "entrypoint": "fetch", "repo_mode": "rw",
            "timeout_s": 600, "egress": "mode"}  # fmt: skip
    normal = _env_for(validate_job(JobSpec.model_validate({**base, "args": {"repo": "o/r"}})), "ids")
    scout = _env_for(
        validate_job(JobSpec.model_validate({**base, "args": {"repo": "o/r", "clone_depth": 1, "prefetch": False}})),
        "ids",
    )
    assert "OSSBOT_ARG_PREFETCH" not in normal
    assert scout["OSSBOT_ARG_PREFETCH"] == "0" and scout["OSSBOT_ARG_CLONE_DEPTH"] == "1"


# ------------------------------------------------------------------- JobSpec rules
def _spec(**kw: Any) -> JobSpec:
    base: dict[str, Any] = {
        "run_id": "r1",
        "cid": "c_01ABCDEFGH",
        "profile": "research",
        "entrypoint": "kiro",
        "args": {"role": "planner", "prompt_file": "plan.md", "model": "claude-test", "effort": "high"},
        "repo_mode": "snapshot",
        "timeout_s": 600,
    }
    base.update(kw)
    return JobSpec.model_validate(base)


@pytest.mark.parametrize(
    ("kw", "ok"),
    [
        ({}, True),
        ({"repo_mode": "rw"}, False),
        ({"args": {"role": "vetter", "prompt_file": "vet.md", "model": "m", "effort": "low"}}, False),
        (
            {"args": {"role": "vetter", "prompt_file": "vet.md", "model": "m", "effort": "low"}, "repo_mode": "none"},
            True,
        ),
        # decision B8: scout is a research role on the read-only snapshot (CR-004 §9.5a)
        ({"args": {"role": "scout", "prompt_file": "scout.md", "model": "m", "effort": "high"}}, True),
        (
            {"args": {"role": "scout", "prompt_file": "scout.md", "model": "m", "effort": "high"}, "repo_mode": "none"},
            False,
        ),
        (
            {
                "profile": "coder",
                "repo_mode": "rw",
                "args": {"role": "scout", "prompt_file": "s.md", "model": "m", "effort": "low"},
            },
            False,
        ),
        ({"args": {"role": "planner", "prompt_file": "../x.md", "model": "m", "effort": "low"}}, False),
        ({"args": {"role": "planner", "prompt_file": "plan.txt", "model": "m", "effort": "low"}}, False),
        ({"args": {"role": "planner", "prompt_file": "plan.md", "model": "-x", "effort": "low"}}, False),
        ({"args": {"role": "planner", "prompt_file": "plan.md", "model": "m", "effort": "low", "agent": "x"}}, False),
        ({"args": {"role": "coder", "prompt_file": "c.md", "model": "m", "effort": "low"}}, False),
        (
            {
                "profile": "coder",
                "repo_mode": "rw",
                "args": {"role": "coder", "prompt_file": "c.md", "model": "m", "effort": "low"},
            },
            True,
        ),
        ({"profile": "catalog", "entrypoint": "list_models", "args": {}, "repo_mode": "none"}, False),
        (
            {
                "profile": "catalog",
                "entrypoint": "list_models",
                "args": {},
                "repo_mode": "none",
                "cid": "sys-catalog",
                "timeout_s": 120,
            },
            True,
        ),
        (
            {
                "profile": "catalog",
                "entrypoint": "list_models",
                "args": {},
                "repo_mode": "none",
                "cid": "sys-catalog",
                "timeout_s": 120,
                "inputs": {"x.md": "/srv/ossbot/runner/x.md"},
            },
            False,
        ),
        ({"profile": "research", "entrypoint": "list_models", "args": {}, "repo_mode": "none"}, False),
        ({"profile": "stack", "entrypoint": "validate", "args": {}, "repo_mode": "rw"}, False),
        ({"entrypoint": "probe", "args": {}}, False),
        ({"timeout_s": 99999}, False),
        (
            {"profile": "fetch", "entrypoint": "fetch", "repo_mode": "rw", "args": {"repo": "a/b"}, "egress": "mode"},
            True,
        ),
        (
            {"profile": "fetch", "entrypoint": "fetch", "repo_mode": "rw", "args": {"repo": "a/b"}, "egress": "strict"},
            True,
        ),
        ({"profile": "fetch", "entrypoint": "fetch", "repo_mode": "rw", "args": {"repo": "a/b"}}, False),
        (
            {"profile": "fetch", "entrypoint": "fetch", "repo_mode": "rw", "args": {"repo": "a/b"}, "egress": "open"},
            False,
        ),
        ({"egress": "mode"}, False),
        (
            {
                "profile": "fetch",
                "entrypoint": "fetch",
                "repo_mode": "rw",
                "args": {"repo": "a/b", "extra": 1},
                "egress": "mode",
            },
            False,
        ),
        (
            {
                "profile": "fetch",
                "entrypoint": "fetch",
                "repo_mode": "rw",
                "args": {"repo": "a/b", "base_sha": "1" * 40},
                "egress": "mode",
            },
            False,
        ),
        (
            {
                "profile": "package",
                "entrypoint": "unbundle",
                "repo_mode": "rw",
                "args": {
                    "mode": "fix_round",
                    "bundle_ref": "refs/heads/ossbot-change",
                    "checkout_sha": "1" * 40,
                    "rev": 2,
                },
            },
            True,
        ),
    ],
)
def test_jobspec_rules(kw: dict[str, Any], ok: bool) -> None:
    if ok:
        validate_job(_spec(**kw))
    else:
        with pytest.raises((SpecError, ValueError)):
            validate_job(_spec(**kw))


def test_jobspec_extra_key_rejected() -> None:
    with pytest.raises(ValueError):
        _spec(privileged=True)


def test_jobspec_limits() -> None:
    with pytest.raises(ValueError):
        _spec(cpus=9)
    with pytest.raises(ValueError):
        _spec(memory_mb=7000)


# ------------------------------------------------------------------ /28 allocation
def test_subnet_allocation_first_free_and_persisted(sbx: Sbx) -> None:
    alloc = SubnetAllocator(sbx.jobs.store, sbx.clock)
    a = alloc.allocate("fetch", "c_A")
    b = alloc.allocate("validate", "c_A")  # shares the fetch pool: next /28
    c = alloc.allocate("research", "c_A")
    d = alloc.allocate("catalog", "sys-catalog")  # agent-llm pool (M11)
    assert (a, b, c, d) == ("10.213.0.0/28", "10.213.0.16/28", "10.213.64.0/28", "10.213.128.0/28")
    assert alloc.allocate("fetch", "c_A") == a  # idempotent
    assert ipaddress.ip_network(alloc.allocate("llm", "c_B")).subnet_of(ipaddress.ip_network("10.213.128.0/18"))
    alloc.release("c_A", "fetch")
    assert alloc.allocate("fetch", "c_C") == "10.213.0.0/28"  # first free index reused
    again = SubnetAllocator(sbx.jobs.store, sbx.clock)  # a new allocator sees the persisted rows
    assert again.allocated("c_A") == {"validate": b, "research": c}
