"""Job admission and the four-step lifecycle over FakeRuntime (DESIGN §10.2 M3, §10.4, §10.6)."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

import pytest

from ossbot.sandboxd.jobs import ApiError
from ossbot.sandboxd.killwatch import KillWatch
from ossbot.sandboxd.profiles import JobSpec
from ossbot.sandboxd.runtime.fake import FakeContainer, FakeRuntime
from tests.conftest import write_flag
from tests.sandboxharness import variants
from tests.unit.sandboxd.conftest import KEY, Sbx, pass_all_probes


def _fetch(sbx: Sbx, run_id: str = "r1", cid: str = "c_01ABCDEFGH", **kw: Any) -> JobSpec:
    return JobSpec.model_validate(
        {
            "run_id": run_id,
            "cid": cid,
            "profile": "fetch",
            "entrypoint": "fetch",
            "args": {"repo": "owner/repo"},
            "repo_mode": "rw",
            "timeout_s": 600,
            "egress": "mode",
            **kw,
        }
    )


def _planner(sbx: Sbx, run_id: str = "r2", model: str = "claude-test") -> JobSpec:
    prompt = sbx.runner_input("plan.md", b"plan this")
    return JobSpec.model_validate(
        {
            "run_id": run_id,
            "cid": "c_01ABCDEFGH",
            "profile": "research",
            "entrypoint": "kiro",
            "args": {"role": "planner", "prompt_file": "plan.md", "model": model, "effort": "high"},
            "repo_mode": "snapshot",
            "inputs": {"plan.md": prompt},
            "timeout_s": 600,
        }
    )


def _names(rt: FakeRuntime, call: str) -> list[str]:
    return [n for c, n in rt.calls if c == call]


def test_fetch_lifecycle(sbx: Sbx) -> None:
    spec = _fetch(sbx, inputs={"issue.json": sbx.runner_input("issue.json", b"{}")})
    view = sbx.jobs.submit(spec)
    assert view["state"] == "succeeded", view
    rt = sbx.runtime
    # stage-in: created, never started, archive PUT, removed
    assert "ossbot-r1-stagein" in _names(rt, "create")
    assert "ossbot-r1-stagein" not in _names(rt, "start")
    assert "ossbot-r1-stagein" in _names(rt, "put_archive")
    staged = rt.volume_dir("ossbot-in-r1") / "issue.json"
    assert staged.read_bytes() == b"{}" and oct(staged.stat().st_mode & 0o777) == "0o444"
    # task network with the proxy attached under alias proxy
    assert "ossbot-t-c-01abcdefgh-fetch" in rt.networks
    assert ("ossbot-t-c-01abcdefgh-fetch", "ossbot-proxy", "proxy") in rt.connections
    # outputs collected to artifacts/<cid>/runs/<run_id>/
    dest = sbx.settings.artifacts_dir / "c_01ABCDEFGH" / "runs" / "r1"
    assert (dest / "result.json").exists()
    assert view["artifact_dir"] == str(dest)
    # containers and per-run volumes removed; task volume kept
    assert rt.containers == {}
    assert {"ossbot-in-r1", "ossbot-out-r1"} <= set(_names(rt, "remove_volume"))
    assert "ossbot-task-c_01abcdefgh" in rt.volumes


# ------------------------------------------------------------------ sandbox-network Part 1 (DESIGN §9.8)
@pytest.mark.parametrize("bad", [{"base_ref": "main"}, {"base_ref": "main", "base_sha": "a" * 40},
                                 {"prefetch": False, "clone_depth": 1}, {"clone_depth": 50}])  # fmt: skip
def test_refetch_args_take_no_base_and_need_the_prefetch(bad: dict[str, Any]) -> None:
    from pydantic import ValidationError

    from ossbot.sandboxd.profiles import FetchArgs

    assert FetchArgs.model_validate({"repo": "owner/repo", "refetch": True}).refetch is True
    with pytest.raises(ValidationError):
        FetchArgs.model_validate({"repo": "owner/repo", "refetch": True, **bad})


def test_refetch_env_only_when_set_and_no_new_task_volume(sbx: Sbx) -> None:
    normal = sbx.jobs.admit(_fetch(sbx))
    assert "OSSBOT_ARG_REFETCH" not in normal.env and normal.create_task_volume is True
    assert normal.env == {"OSSBOT_ARG_REPO": "owner/repo", "OSSBOT_ARG_CLONE_DEPTH": "200"}  # unchanged argv
    again = sbx.jobs.admit(_fetch(sbx, run_id="r9", args={"repo": "owner/repo", "refetch": True}))
    assert again.env == {"OSSBOT_ARG_REPO": "owner/repo", "OSSBOT_ARG_CLONE_DEPTH": "200", "OSSBOT_ARG_REFETCH": "1"}
    assert again.create_task_volume is False  # keeps the implement stage's working tree


def test_refetch_job_creates_no_task_volume(sbx: Sbx) -> None:
    assert sbx.jobs.submit(_fetch(sbx))["state"] == "succeeded"
    view = sbx.jobs.submit(_fetch(sbx, run_id="r2", args={"repo": "owner/repo", "refetch": True}))
    assert view["state"] == "succeeded", view
    assert _names(sbx.runtime, "create_volume").count("ossbot-task-c_01abcdefgh") == 1  # the fetch's only


@pytest.mark.parametrize("profile", ["fetch", "validate"])
def test_repo_code_job_without_egress_is_refused(sbx: Sbx, profile: str) -> None:
    """Open-discovery design §12.3: fail closed, the egress decision is required, never defaulted."""
    body: dict[str, Any] = {"run_id": "r1", "cid": "c_01ABCDEFGH", "profile": profile, "entrypoint": profile,
                            "args": {"repo": "owner/repo"} if profile == "fetch" else {}, "repo_mode": "rw",
                            "timeout_s": 600}  # fmt: skip
    with pytest.raises(ApiError) as exc:
        sbx.jobs.submit(JobSpec.model_validate(body))
    assert (exc.value.status, exc.value.error_class) == (400, "egress_required")
    assert sbx.runtime.calls == []


def test_egress_on_another_profile_is_refused(sbx: Sbx) -> None:
    spec = _planner(sbx).model_copy(update={"egress": "mode"})
    with pytest.raises(ApiError) as exc:
        sbx.jobs.submit(spec)
    assert (exc.value.status, exc.value.error_class) == (400, "egress_not_applicable")
    assert sbx.runtime.calls == []


@pytest.mark.parametrize(
    ("profile", "egress", "network", "pool"),
    [
        ("fetch", "mode", "ossbot-t-c-01abcdefgh-fetch", "10.213.0.0/18"),
        ("fetch", "strict", "ossbot-t-c-01abcdefgh-fetch-strict", "10.213.192.0/19"),
        ("validate", "mode", "ossbot-t-c-01abcdefgh-validate", "10.213.0.0/18"),
        ("validate", "strict", "ossbot-t-c-01abcdefgh-validate-strict", "10.213.192.0/19"),
    ],
)
def test_strict_jobs_get_a_28_in_the_strict_pool(sbx: Sbx, profile: str, egress: str, network: str, pool: str) -> None:
    """A strict job's network profile is <net>-strict (/28 in pool_strict, the base allowlist in every
    egress mode); a mode job keeps fetch/validate in 10.213.0.0/18. The fake runtime runs the Docker
    filter rules, so the -strict network name and subnet pass the real create checks."""
    if profile == "fetch":
        spec = _fetch(sbx, egress=egress)
    else:
        plan = sbx.runner_input("validate.json", b'{"commands": []}')
        spec = JobSpec.model_validate({"run_id": "r1", "cid": "c_01ABCDEFGH", "profile": "validate",
                                       "entrypoint": "validate", "args": {}, "repo_mode": "rw", "timeout_s": 600,
                                       "inputs": {"validate.json": plan}, "egress": egress})  # fmt: skip
    assert sbx.jobs.submit(spec)["state"] == "succeeded"
    rt = sbx.runtime
    assert network in rt.networks and (network, "ossbot-proxy", "proxy") in rt.connections
    subnet = ipaddress.ip_network(rt.networks[network])
    assert subnet.prefixlen == 28 and subnet.subnet_of(ipaddress.ip_network(pool))
    other = network.removesuffix("-strict") if egress == "strict" else f"{network}-strict"
    assert other not in rt.networks
    allocated = sbx.jobs.networks.subnets.allocated("c_01ABCDEFGH")
    assert set(allocated) == {network.removeprefix("ossbot-t-c-01abcdefgh-")}


def test_one_job_per_cid(sbx: Sbx) -> None:
    sbx.jobs._active["c_01ABCDEFGH"] = "other"
    with pytest.raises(ApiError) as exc:
        sbx.jobs.submit(_fetch(sbx))
    assert (exc.value.status, exc.value.error_class) == (409, "cid_busy")


def test_run_id_unique(sbx: Sbx) -> None:
    sbx.jobs.submit(_fetch(sbx))
    with pytest.raises(ApiError) as exc:
        sbx.jobs.submit(_fetch(sbx))
    assert exc.value.status == 409


@pytest.mark.parametrize("flag", ["paused", "killed"])
def test_refused_while_paused_or_killed(sbx: Sbx, flag: str) -> None:
    write_flag(sbx.settings.control_dir / flag, {"level": "soft", "reason": "x"})
    with pytest.raises(ApiError) as exc:
        sbx.jobs.submit(_fetch(sbx))
    assert (exc.value.status, exc.value.error_class) == (423, flag)
    assert sbx.runtime.calls == []


def test_input_outside_roots_rejected(sbx: Sbx, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x")
    with pytest.raises(ApiError) as exc:
        sbx.jobs.submit(_fetch(sbx, inputs={"x.txt": str(outside)}))
    assert exc.value.error_class == "bad_input"


def test_symlinked_input_fails_stage_in(sbx: Sbx, tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("s")
    link = Path(sbx.runner_input("real.txt", b"x")).with_name("link.txt")
    link.symlink_to(secret)
    view = sbx.jobs.submit(_fetch(sbx, inputs={"link.txt": str(link)}))
    assert (view["state"], view["error_class"]) == ("failed", "stage_in")


def test_research_snapshot_and_postcheck(sbx: Sbx) -> None:
    pass_all_probes(sbx)
    view = sbx.jobs.submit(_planner(sbx))
    assert view["state"] == "succeeded", view
    created = _names(sbx.runtime, "create")
    assert created.index("ossbot-r2-snapshot") < created.index("ossbot-r2-snapcheck") < created.index("ossbot-r2")
    assert "ossbot-ro-r2" in _names(sbx.runtime, "remove_volume")


def test_snapshot_unsafe_pauses(sbx: Sbx) -> None:
    pass_all_probes(sbx)

    def check(rt: FakeRuntime, c: FakeContainer) -> int:
        if c.spec.name.endswith("-snapcheck"):
            c.logs = b"/work/repo/docs/env\n"
        return 0

    sbx.runtime.handlers["probe"] = check
    view = sbx.jobs.submit(_planner(sbx))
    assert (view["state"], view["error_class"]) == ("failed", "snapshot_unsafe")
    assert sbx.gatekeeper.paused == ["snapshot_unsafe"]
    assert sbx.runtime.containers == {}


def test_secret_in_output_fails_and_pauses(sbx: Sbx) -> None:
    def leak(rt: FakeRuntime, c: FakeContainer) -> int:
        out = c.volume_path(rt, "/io/output")
        assert out is not None
        (out / "plan.json").write_text("token " + KEY[::-1])
        return 0

    sbx.runtime.handlers["fetch"] = leak
    view = sbx.jobs.submit(_fetch(sbx))
    assert (view["state"], view["error_class"]) == ("failed", "secret_in_output")
    assert sbx.gatekeeper.paused == ["secret in output"]
    assert not (sbx.settings.artifacts_dir / "c_01ABCDEFGH" / "runs" / "r1").exists()


def test_secret_in_logs_redacted(sbx: Sbx) -> None:
    def leak(rt: FakeRuntime, c: FakeContainer) -> int:
        c.logs = f"debug {KEY}\n".encode()
        return 0

    sbx.runtime.handlers["fetch"] = leak
    view = sbx.jobs.submit(_fetch(sbx))
    assert view["error_class"] == "secret_in_output"
    log = (sbx.settings.logs_dir / "runs" / "r1.log").read_bytes()
    assert KEY.encode() not in log and b"[REDACTED]" in log


def test_coder_pair(sbx: Sbx) -> None:
    pass_all_probes(sbx)
    prompt = sbx.runner_input("code.md", b"implement")
    spec = JobSpec.model_validate(
        {
            "run_id": "r3",
            "cid": "c_01ABCDEFGH",
            "profile": "coder",
            "entrypoint": "kiro",
            "args": {"role": "coder", "prompt_file": "code.md", "model": "claude-test", "effort": "high"},
            "repo_mode": "rw",
            "inputs": {"code.md": prompt},
            "timeout_s": 600,
            "egress": "mode",
        }
    )
    view = sbx.jobs.submit(spec)
    assert view["state"] == "succeeded", view
    assert view["exec_network"] == "none" and sbx.gatekeeper.egress_calls == []  # mode allowlist: never asks
    rt = sbx.runtime
    starts = _names(rt, "start")
    assert starts.index("ossbot-r3-exec") < starts.index("ossbot-r3")  # agent-exec first (M4 readiness)
    assert "ossbot-r3-exec" in _names(rt, "kill")
    assert "ossbot-t-c-01abcdefgh-llm" in rt.networks
    llm_argv = next(
        a for a in rt.argvs if "ossbot-r3" in a and "--name" in a and a[a.index("--name") + 1] == "ossbot-r3"
    )
    assert "KIRO_API_KEY" in llm_argv and "REQUIRE_MCP=1" in llm_argv
    exec_argv = next(a for a in rt.argvs if a[a.index("--name") + 1] == "ossbot-r3-exec")
    assert "KIRO_API_KEY" not in exec_argv and exec_argv[exec_argv.index("--network") + 1] == "none"
    assert (sbx.settings.artifacts_dir / "c_01ABCDEFGH" / "runs" / "r3" / "exec").is_dir()


# ------------------------------------------------------------- ADR-0024: the coder's executor network
def _coder(sbx: Sbx, run_id: str = "r3", egress: str | None = "mode") -> JobSpec:
    body: dict[str, Any] = {
        "run_id": run_id, "cid": "c_01ABCDEFGH", "profile": "coder", "entrypoint": "kiro",
        "args": {"role": "coder", "prompt_file": "code.md", "model": "claude-test", "effort": "high"},
        "repo_mode": "rw", "inputs": {"code.md": sbx.runner_input("code.md", b"implement")}, "timeout_s": 600,
    }  # fmt: skip
    if egress is not None:
        body["egress"] = egress
    return JobSpec.model_validate(body)


def _argv(rt: FakeRuntime, name: str) -> list[str]:
    return next(a for a in rt.argvs if "--name" in a and a[a.index("--name") + 1] == name)


def _llm_shape(argv: list[str]) -> list[str]:
    """agent-llm's argv without the run-specific names (byte-identical across exec networks)."""
    return [a.replace("r3", "R").replace("r4", "R") for a in argv]


def test_coder_without_egress_is_refused(sbx: Sbx) -> None:
    pass_all_probes(sbx)
    with pytest.raises(ApiError) as exc:
        sbx.jobs.submit(_coder(sbx, egress=None))
    assert (exc.value.status, exc.value.error_class) == (400, "egress_required")
    assert sbx.runtime.calls == [] and sbx.gatekeeper.egress_calls == []


def test_networked_agent_exec_when_mode_and_gatekeeper_agree(sbx: Sbx) -> None:
    pass_all_probes(sbx)
    sbx.mode[0] = "sandbox_open"
    sbx.gatekeeper.egress_answer = "web"
    view = sbx.jobs.submit(_coder(sbx))
    assert view["state"] == "succeeded" and view["exec_network"] == "web"
    assert sbx.gatekeeper.egress_calls == [("c_01ABCDEFGH", "r3")]
    rt = sbx.runtime
    exec_argv = _argv(rt, "ossbot-r3-exec")
    assert exec_argv[exec_argv.index("--network") + 1] == "ossbot-t-c-01abcdefgh-exec"
    assert "OSSBOT_EXEC_NETWORK=web" in exec_argv and "KIRO_API_KEY" not in exec_argv
    assert exec_argv[exec_argv.index("--dns") + 1] == "127.0.0.1"
    subnet = ipaddress.ip_network(rt.networks["ossbot-t-c-01abcdefgh-exec"])
    assert subnet.prefixlen == 28 and subnet.subnet_of(ipaddress.ip_network("10.213.0.0/18"))
    assert ("ossbot-t-c-01abcdefgh-exec", "ossbot-proxy", "proxy") in rt.connections
    llm_argv = _argv(rt, "ossbot-r3")
    assert llm_argv[llm_argv.index("--network") + 1] == "ossbot-t-c-01abcdefgh-llm"
    assert "OSSBOT_EXEC_NETWORK=web" not in llm_argv


@pytest.mark.parametrize(
    ("mode", "answer", "egress", "calls"),
    [
        ("sandbox_open", "web", "strict", 0),  # strict: never asks, never networked
        ("open", "web", "mode", 0),  # sandboxd's own mode read is not sandbox_open: no call
        ("allowlist", "web", "mode", 0),
        ("sandbox_open", "none", "mode", 1),
        ("sandbox_open", RuntimeError("transport"), "mode", 1),
        ("sandbox_open", ValueError("bad json"), "mode", 1),
        ("sandbox_open", "WEB", "mode", 1),  # junk
    ],
)
def test_agent_exec_stays_offline_otherwise(sbx: Sbx, mode: str, answer: Any, egress: str, calls: int,
                                            caplog: pytest.LogCaptureFixture) -> None:  # fmt: skip
    pass_all_probes(sbx)
    sbx.jobs.submit(_coder(sbx, "r4", egress="mode"))  # the reference: offline (mode allowlist)
    reference = _llm_shape(_argv(sbx.runtime, "ossbot-r4"))
    sbx.runtime.argvs.clear()
    sbx.mode[0] = mode
    sbx.gatekeeper.egress_answer = answer
    with caplog.at_level("WARNING"):
        view = sbx.jobs.submit(_coder(sbx, "r3", egress=egress))
    assert view["state"] == "succeeded" and view["exec_network"] == "none"
    assert len(sbx.gatekeeper.egress_calls) == calls
    exec_argv = _argv(sbx.runtime, "ossbot-r3-exec")
    assert exec_argv[exec_argv.index("--network") + 1] == "none"
    assert not any(a.startswith("OSSBOT_EXEC_NETWORK") for a in exec_argv)
    assert "ossbot-t-c-01abcdefgh-exec" not in sbx.runtime.networks
    assert _llm_shape(_argv(sbx.runtime, "ossbot-r3")) == reference  # agent-llm byte-identical
    if isinstance(answer, Exception):
        assert any(getattr(r, "event", "") == "implement_egress_unavailable" for r in caplog.records)


def test_agent_llm_is_identical_with_a_networked_exec(sbx: Sbx) -> None:
    pass_all_probes(sbx)
    sbx.jobs.submit(_coder(sbx, "r4"))
    reference = _llm_shape(_argv(sbx.runtime, "ossbot-r4"))
    sbx.mode[0] = "sandbox_open"
    sbx.gatekeeper.egress_answer = "web"
    sbx.jobs.submit(_coder(sbx, "r3"))
    assert _llm_shape(_argv(sbx.runtime, "ossbot-r3")) == reference


def test_coder_refused_while_a_hold_is_pending(sbx: Sbx) -> None:
    pass_all_probes(sbx)
    sbx.jobs._pending("c_01ABCDEFGH", "r0", {"x": 1}, None)
    with pytest.raises(ApiError) as exc:
        sbx.jobs.submit(_coder(sbx))
    assert (exc.value.status, exc.value.error_class) == (423, "egress_anomaly_pending")
    assert sbx.gatekeeper.egress_calls == []


def test_probe_coder_plans_never_ask(sbx: Sbx) -> None:
    """The safety probes build coder Plans directly (no admit()): always offline, no decision call."""
    from ossbot.sandboxd.jobs import ProbeRunner

    plan = ProbeRunner(sbx.jobs)._plan("coder_surface", "coder", "kiro", "rw", {"ROLE": "coder"})
    assert plan.exec_network == "none" and plan.exec_env == {}
    assert sbx.gatekeeper.egress_calls == []


def test_package_artifacts_go_to_rev_dir(sbx: Sbx) -> None:
    spec = JobSpec.model_validate(
        {
            "run_id": "r4",
            "cid": "c_01ABCDEFGH",
            "profile": "package",
            "entrypoint": "package",
            "args": {
                "rev": 2,
                "round_base": "1" * 40,
                "package_ts": "2026-10-17T12:00:00Z",
                "name": "Test Person",
                "email": "test@example.invalid",
            },
            "repo_mode": "rw",
            "inputs": {"commit_message.txt": sbx.runner_input("commit_message.txt", b"fix: x\n")},
            "timeout_s": 600,
        }
    )
    view = sbx.jobs.submit(spec)
    assert view["artifact_dir"] == str(sbx.settings.artifacts_dir / "c_01ABCDEFGH" / "2")
    argv = sbx.runtime.argvs[-1]
    assert "OSSBOT_ARG_ROUND_BASE=" + "1" * 40 in argv and argv[argv.index("--network") + 1] == "none"


def test_catalog_job_releases_its_network(sbx: Sbx) -> None:
    spec = JobSpec.model_validate(
        {
            "run_id": "cat1",
            "cid": "sys-catalog",
            "profile": "catalog",
            "entrypoint": "list_models",
            "args": {},
            "repo_mode": "none",
            "timeout_s": 120,
        }
    )
    assert sbx.jobs.submit(spec)["state"] == "succeeded"
    assert "ossbot-t-sys-catalog-catalog" in _names(sbx.runtime, "create_network")
    assert "ossbot-t-sys-catalog-catalog" in _names(sbx.runtime, "remove_network")
    assert sbx.jobs.networks.subnets.allocated("sys-catalog") == {}


def test_nonzero_exit_records_error_class(sbx: Sbx) -> None:
    def failing(rt: FakeRuntime, c: FakeContainer) -> int:
        out = c.volume_path(rt, "/io/output")
        assert out is not None
        (out / "result.json").write_text('{"ok":false,"error_class":"base_mismatch"}')
        return 3

    sbx.runtime.handlers["fetch"] = failing
    view = sbx.jobs.submit(_fetch(sbx))
    assert (view["state"], view["exit_code"], view["error_class"]) == ("failed", 3, "base_mismatch")


def test_destroy_only_terminal(sbx: Sbx) -> None:
    sbx.jobs.submit(_fetch(sbx))
    sbx.gatekeeper.states["c_01ABCDEFGH"] = "IMPLEMENTING"
    with pytest.raises(ApiError) as exc:
        sbx.jobs.destroy_task("c_01ABCDEFGH")
    assert exc.value.error_class == "not_terminal"
    for after_start in ("PROPOSAL_APPROVED", "FETCHING", "RESCOPE_REQUIRED", "WATCHING"):
        sbx.gatekeeper.states["c_01ABCDEFGH"] = after_start
        with pytest.raises(ApiError):
            sbx.jobs.destroy_task("c_01ABCDEFGH")
    sbx.gatekeeper.states["c_01ABCDEFGH"] = "PROPOSAL_SNOOZED"  # gate 1 pending: only the scout clone (§9.5a)
    sbx.jobs.destroy_task("c_01ABCDEFGH")
    assert "ossbot-task-c_01abcdefgh" not in sbx.runtime.volumes
    sbx.jobs.submit(_fetch(sbx, run_id="r2"))
    sbx.gatekeeper.states["c_01ABCDEFGH"] = "MERGED"
    sbx.jobs.destroy_task("c_01ABCDEFGH")
    assert "ossbot-task-c_01abcdefgh" not in sbx.runtime.volumes
    assert "ossbot-t-c-01abcdefgh-fetch" not in sbx.runtime.networks
    assert sbx.jobs.networks.subnets.allocated("c_01ABCDEFGH") == {}
    with pytest.raises(ApiError):
        sbx.jobs.destroy_task("c_UNKNOWN000")


def test_killwatch_kills_running_jobs_on_kill_only(sbx: Sbx) -> None:
    spec = variants()["fetch"]
    sbx.runtime.volumes.update({"ossbot-in-r1-plan", "ossbot-out-r1-plan"})
    sbx.runtime.handlers["fetch"] = lambda rt, c: -1  # keeps running
    sbx.runtime.run(spec)
    sbx.jobs._containers["r1-plan"] = [spec.name]
    watch = KillWatch(sbx.jobs, sbx.settings.control_dir)
    write_flag(sbx.settings.control_dir / "paused", {"reason": "x"})
    assert watch.tick() == 0  # paused: running jobs finish, new ones are refused
    write_flag(sbx.settings.control_dir / "killed", {"level": "soft", "reason": "x"})
    assert watch.tick() == 1
    assert ("kill", spec.name) in sbx.runtime.calls
    assert "r1-plan" in sbx.jobs._cancelled
