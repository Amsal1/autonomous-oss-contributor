"""DESIGN §22.5 sandbox isolation smoke tests, one container per profile via sandboxd's templates.

Every bullet that needs no Kiro key runs on this suite's own JobService (no key). The two
``needs_key`` cases run against the real key through product code: the Kiro host CONNECT through
``ossctl kiro smoke`` and the live probe results, S-ws-1 through the live ``workspace`` probe
(a real planner and coder run over the hostile fixture) for the current tuple.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Literal

import pytest

from tests.sandbox.conftest import Sandbox, proxy_ip_on

Pool = Literal["fetch", "strict", "research", "llm"]
SQUID_CONF = Path("/etc/ossbot/proxy/squid.conf")

CID = "c_SMOKETEST1"
PEER_CID = "c_SMOKETEST2"
SNAP_CID = "c_SMOKETEST3"
FETCH_CID = "c_SMOKETEST4"
STRICT_CID = "c_SMOKETEST5"
LIVE_SANDBOXD = Path("/run/ossbot/sandboxd.sock")
OSSCTL = Path("/opt/ossbot/app/.venv/bin/ossctl")
_cache: dict[str, dict[str, Any]] = {}
# /work holds nothing but the empty image directory /work/repo (the mountpoint research uses).
EMPTY_WORK = (None, [], ["/work/repo"])


def facts(sandbox: Sandbox, template: str, **kw: Any) -> dict[str, Any]:
    key = template + json.dumps(kw, sort_keys=True, default=str)
    if key not in _cache:
        _cache[key] = sandbox.smoke(kw.pop("cid", CID), template, **kw)
    return _cache[key]


def live_mode() -> str:
    """The egress mode the live proxy was rendered for (header line of the rendered squid.conf)."""
    match = re.search(
        r"^# Egress mode: (allowlist|research|open|sandbox_open) ", SQUID_CONF.read_text(encoding="utf-8"), re.M
    )
    assert match, f"{SQUID_CONF} names no egress mode (redeploy)"
    return match.group(1)


def expected_egress(pool: Pool, mode: str) -> tuple[bool, bool]:
    """(web_public, github_user_content) of a source pool in an egress mode (DESIGN §11.3, §11.5;
    open-discovery design §12.2): research has github_user_content in every mode and web_public outside
    allowlist; the llm pool (agent-llm, catalog) has web_public (which covers github.io and gists) in
    open and sandbox_open; the fetch pool (fetch/validate) only in sandbox_open; the strict pool never."""
    if pool == "research":
        return mode != "allowlist", True
    if pool == "llm":
        web = mode in {"open", "sandbox_open"}
        return web, web
    if pool == "fetch":
        return mode == "sandbox_open", mode == "sandbox_open"
    return False, False


def assert_common(f: dict[str, Any], networked: bool, pool: Pool = "fetch") -> None:
    """The 'All' bullet of §22.5. example.com through the proxy is 403 unless the pool has
    web_public in the live egress mode (§11.3: any host minus the always-denied set)."""
    assert f["uid"] == 10001
    assert int(f["cap_eff"], 16) == 0
    assert f["root_writable"] is False
    assert f["input_writable"] is False
    assert f["output_writable"] is True
    assert f["docker_sock"] is False
    assert f["run_ossbot"] is False
    assert f["mnt_entries"] == []
    assert f["resolves_example"] is False
    assert f["dns_txt_embedded"] in {"empty", "no_reply"}
    assert not [n for n in f["env_names"] if n.startswith(("GITHUB", "TELEGRAM"))]
    assert f["kiro_direct"] != "open"
    if networked:
        mode = live_mode()
        web, user_content = expected_egress(pool, mode)
        assert f["proxy_example"]["connect"] == ("200" if web else "403"), (pool, mode)
        assert f["noproxy_example"]["curl_exit"] != 0
        assert all(v != "open" for v in f["host_ports"].values()), f["host_ports"]
        # proxy:3140/3141/3150 through the task-network alias: unreachable, refused or 403 (N10)
        for port, res in f["alias_ports"].items():
            assert res["connect"] in {"000", "403"}, (port, res)
        # CR-002: github.io and gists for research in every mode; never for fetch/validate
        for key in ("gist", "gist_raw", "ghio"):
            assert f[key]["connect"] == ("200" if user_content else "403"), (key, pool, mode, f[key])
        assert f["telegraph"]["connect"] == "403", (pool, mode)  # always denied (CR-003 U11)
    else:
        assert f["interfaces"] == ["lo"]


@pytest.fixture(scope="module")
def peer_ip(sandbox: Sandbox) -> str:
    net = sandbox.svc.networks.ensure(PEER_CID, "fetch")
    return proxy_ip_on(net)


@pytest.mark.parametrize("template", ["fetch", "validate"])
def test_fetch_validate(sandbox: Sandbox, peer_ip: str, template: str) -> None:
    f = facts(sandbox, template, repo_mode="rw", create_task=True, env={"OSSBOT_PROBE_PEER": peer_ip})
    assert_common(f, networked=True)
    assert f["github"]["connect"] == "200"
    assert "KIRO_API_KEY" not in f["env_names"]
    assert f["peer"] != "open"  # another task's network is unreachable
    assert f["tmp_exec"] == 0  # S-perm-1: validate builds and executes /tmp/bin/x
    assert f["volume_modes"]["/io/output"] == "10001:700"


@pytest.mark.parametrize("template", ["fetch", "validate"])
def test_strict_pool_keeps_the_base_list_in_every_mode(sandbox: Sandbox, template: str) -> None:
    """Open-discovery design §12.3: a strict job's /28 is in pool_strict (10.213.192.0/19): github_read
    and the package categories, never web_public, whatever the live egress mode."""
    try:
        f = sandbox.smoke(STRICT_CID, template, repo_mode="rw", create_task=True, egress="strict")
        assert_common(f, networked=True, pool="strict")
        assert f["github"]["connect"] == "200"
        assert "KIRO_API_KEY" not in f["env_names"]
        allocated = sandbox.svc.networks.subnets.allocated(STRICT_CID)
        assert set(allocated) == {f"{template}-strict"}
        assert ipaddress.ip_network(allocated[f"{template}-strict"]).subnet_of(ipaddress.ip_network("10.213.192.0/19"))
    finally:
        sandbox.cleanup(STRICT_CID)


def test_research(sandbox: Sandbox) -> None:
    f = facts(sandbox, "research")
    assert_common(f, networked=True, pool="research")
    web = expected_egress("research", live_mode())[0]
    assert f["github"]["connect"] == ("200" if web else "403")  # via web_public only; no token present
    assert f["home"] == "/home/agent" and f["home_writable"] is True
    assert f["tmp_exec"] != 0  # noexec /tmp in key-holding profiles (M2)
    assert f["work_entries"] in EMPTY_WORK


def test_agent_llm_and_catalog(sandbox: Sandbox) -> None:
    for template, cid in (("catalog", "sys-catalog"),):
        f = facts(sandbox, template, cid=cid)
        assert_common(f, networked=True, pool="llm")
        # no github_read for the llm pool; github.com only through web_public in egress mode open/sandbox_open
        assert f["github"]["connect"] == ("200" if expected_egress("llm", live_mode())[0] else "403")
        assert f["tmp_exec"] != 0
        assert f["mounts_task"] is False and f["mounts_ro"] is False


def test_package_no_network(sandbox: Sandbox) -> None:
    f = facts(sandbox, "package", repo_mode="rw", create_task=True)
    assert_common(f, networked=False)
    assert f["tmp_exec"] == 0


def test_stage_in_and_hostile_job(sandbox: Sandbox) -> None:
    """M3: inputs arrive as regular 0444 files identical to the runner's; a hostile job's
    /work/input and /work/output symlinks on the task volume change nothing for the next job."""
    data = b'{"commands": []}\n'
    src = sandbox.root / "inputs" / "plan.json"
    src.write_bytes(data)
    hostile = sandbox.run(sandbox.plan(CID, "validate", repo_mode="rw", env={"OSSBOT_PROBE": "hostile"}))
    assert hostile.exit_code == 0, hostile.logs[-2000:]
    f = sandbox.smoke(CID, "fetch", repo_mode="rw", inputs={"plan.json": src})
    entry = f["inputs"]["plan.json"]
    assert entry == {"regular": True, "mode": "444", "sha256": hashlib.sha256(data).hexdigest()}
    assert f["output_writable"] is True


def test_symlink_snapshot_and_execd(sandbox: Sandbox) -> None:
    """S-symlink-1 (H1): the research snapshot has no symlinks/FIFOs, the stub text is present,
    docs/env is gone; execd refuses docs/env (ELINK) and a symlinked parent (EPATH); S-key-1 facts
    from inside agent-exec; S-perm-1 execd binds /run/exec/exec.sock."""
    sandbox.fixture_task_volume(SNAP_CID)
    hostile = sandbox.run(sandbox.plan(SNAP_CID, "validate", repo_mode="rw", env={"OSSBOT_PROBE": "hostile"}))
    assert hostile.exit_code == 0, hostile.logs[-2000:]
    f = sandbox.smoke(SNAP_CID, "research", repo_mode="snapshot")
    assert_common(f, networked=True, pool="research")
    assert f["work_nonregular"] == []
    assert f["symlink_stub"] == "SYMLINK -> /proc/self/environ"
    assert f["docs_env_present"] is False
    assert f["repo_writable"] is False
    assert not any(e.endswith(("/fifo", "/linkdir")) for e in f["work_entries"] or [])
    out = sandbox.run(sandbox.plan(SNAP_CID, "coder", repo_mode="rw", env={"OSSBOT_PROBE": "execd"}))
    assert out.exit_code == 0, out.logs[-3000:]
    replies = json.loads(out.files["execd.json"])
    assert replies["1"]["ok"] is False and replies["1"]["error"]["code"] == "ELINK"
    assert replies["2"]["ok"] is False and replies["2"]["error"]["code"] == "EPATH"
    assert replies["3"]["ok"] is True
    assert "hi" in json.dumps(replies["4"]["result"])
    s_key = replies["5"]["result"]["stdout"].split()
    assert "key_hits=0" in s_key
    assert "ifaces=lo" in s_key  # no network interface but loopback
    assert "direct=closed" in s_key
    # agent-llm (the key-holding half of the coder pair): no repo, no task/ro volume, noexec /tmp.
    llm = sandbox.smoke(SNAP_CID, "coder", repo_mode="rw")
    assert_common(llm, networked=True, pool="llm")
    assert llm["github"]["connect"] == ("200" if expected_egress("llm", live_mode())[0] else "403")
    assert llm["work_entries"] in EMPTY_WORK and llm["mounts_task"] is False and llm["mounts_ro"] is False
    assert llm["tmp_exec"] != 0
    assert llm["volume_modes"]["/run/exec"] is not None


def test_fetch_clones(sandbox: Sandbox) -> None:
    """S-perm-1: a real fetch job creates /work/cache and clones into /work/repo through the proxy."""
    plan = sandbox.plan(FETCH_CID, "fetch", entrypoint="fetch", repo_mode="rw", create_task=True,
                        env={"OSSBOT_ARG_REPO": "octocat/Hello-World", "OSSBOT_ARG_CLONE_DEPTH": "1"})  # fmt: skip
    out = sandbox.run(plan)
    assert out.exit_code == 0, out.logs[-2000:]
    result = json.loads(out.files["result.json"])
    assert result["ok"] is True and len(result["head_sha"]) == 40
    f = sandbox.smoke(FETCH_CID, "validate", repo_mode="rw")
    assert "cache" in f["work_top"] and "repo" in f["work_top"]
    assert "README" in f["repo_top"] and ".git" in f["repo_top"]


EXEC_GRADLE_CID = "c_SMOKETEST9"
EXEC_MAVEN_CID = "c_SMOKETESTG"


def execd_runs(sandbox: Sandbox, cid: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    """The offline agent-exec of the coder pair (``--network none``, env -i + ``fixed_env()``) runs ``runs``
    through execd (probe mode ``execd_run`` on the key-free agent-llm side); the answers by id."""
    src = sandbox.root / "inputs" / cid / "execd-run.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(json.dumps(runs), encoding="utf-8")
    out = sandbox.run(sandbox.plan(cid, "coder", repo_mode="rw", env={"OSSBOT_PROBE": "execd_run"},
                                   inputs={"execd-run.json": src}, timeout=2700))  # fmt: skip
    assert out.exit_code == 0, out.logs[-3000:]
    replies: dict[str, Any] = json.loads(out.files["execd.json"])
    return replies


def test_offline_agent_exec_builds_gradle_after_the_refetch(sandbox: Sandbox) -> None:
    """sandbox-network Part 1 (SN-1, SN-2): the env -i + fixed_env() path that failed for kestra
    (GRADLE_OPTS=--offline) builds a multi-project Gradle repo offline; pipefail reports a failed pipe."""
    from tests.sandbox.test_jvm_offline import prepare

    try:
        prepare(sandbox, EXEC_GRADLE_CID, "gradle-multi")
        replies = execd_runs(sandbox, EXEC_GRADLE_CID, [
            {"id": 1, "cmd": "bash ./gradlew --no-daemon --offline :app:test", "timeout_s": 1200},
            {"id": 2, "cmd": "false | cat", "timeout_s": 30},
            {"id": 3, "cmd": "bash ./gradlew --no-daemon :lib:compileJava", "timeout_s": 600},
        ])  # fmt: skip
        assert replies["1"]["ok"] is True and replies["1"]["result"]["exit_code"] == 0, replies["1"]
        assert replies["2"]["result"]["exit_code"] == 1  # pipefail
        assert replies["3"]["result"]["exit_code"] == 0, replies["3"]  # offline by itself (the init script)
    finally:
        sandbox.cleanup(EXEC_GRADLE_CID)


def test_offline_agent_exec_runs_maven_tests_after_validate(sandbox: Sandbox) -> None:
    """SN-4: after a validate filled /work/cache/m2 (surefire's providers resolve only when tests run),
    ``mvn -o test`` in the offline agent-exec passes through ~/.m2/settings.xml (offline, local repository)."""
    from tests.sandbox.test_jvm_offline import prepare, validate

    try:
        prepare(sandbox, EXEC_MAVEN_CID, "maven-multi")
        _, done = validate(sandbox, EXEC_MAVEN_CID, [{"name": "mvn-test", "cmd": "mvn -B -ntp test",
                                                      "timeout_s": 1200}])  # fmt: skip
        assert done["mvn-test"]["exit_code"] == 0, done["mvn-test"]
        replies = execd_runs(sandbox, EXEC_MAVEN_CID, [
            {"id": 1, "cmd": "mvn -B -ntp -o test", "timeout_s": 1200},
            {"id": 2, "cmd": "mvn -B -ntp test", "timeout_s": 1200},
        ])  # fmt: skip
        assert replies["1"]["result"]["exit_code"] == 0, replies["1"]
        assert replies["2"]["result"]["exit_code"] == 0, replies["2"]  # offline through settings.xml alone
    finally:
        sandbox.cleanup(EXEC_MAVEN_CID)


def live_probes() -> dict[str, Any]:
    """``GET /v1/probes/status`` of the live sandboxd (root may read it, like ``ossctl doctor``),
    checked to be for the images and agent configs this suite runs against."""
    import httpx

    from ossbot.common.images import DEFAULT_LOCK_COPY, load_image_pins
    from ossbot.common.uds import uds_client
    from ossbot.sandboxd.probes import compute_key

    if not LIVE_SANDBOXD.exists():
        pytest.fail(f"{LIVE_SANDBOXD} missing: provision first")
    with uds_client(LIVE_SANDBOXD, timeout=httpx.Timeout(10.0)) as client:
        resp = client.get("/v1/probes/status")
    assert resp.status_code == 200, resp.text
    doc: dict[str, Any] = resp.json()
    key = compute_key(load_image_pins(DEFAULT_LOCK_COPY), doc["trust_mode"] or "ids")
    assert key is not None and doc["probe_key"] == key.sha256, "live sandboxd runs other images: redeploy"
    return doc


def test_kiro_host_connect() -> None:
    """research and agent-llm reach the Kiro host with the real key (needs_key, now live): ``ossctl
    kiro smoke`` (one research-profile job on roles.test) answers pong, and the probes that need a
    model answer passed for the current tuple: research_deny (a research session) and coder_surface
    (the coder pair's agent-llm). Both go through the proxy; direct egress stays closed (assert_common)."""
    proc = subprocess.run([str(OSSCTL), "kiro", "smoke"], capture_output=True, text=True, timeout=900, check=False)
    assert proc.returncode == 0, (proc.stdout[-1500:], proc.stderr[-1500:])
    probes = live_probes()["probes"]
    assert probes["research_deny"] is True and probes["coder_surface"] is True, probes


def test_workspace_canary_s_ws_1() -> None:
    """S-ws-1 (needs_key, now live): the ``workspace`` safety probe runs a real planner and a real coder
    session with the key over the hostile fixture (.kiro/settings/mcp.json, hooks, AGENTS.md) and
    fails on the canary file, the canary host in the proxy log or a key leak (``eval_workspace``).
    It passed for the current tuple, the images and agent configs this suite tests."""
    doc = live_probes()
    assert doc["probes"]["workspace"] is True, "workspace probe not passed: ossctl doctor --probes"
    assert doc["all_passed"] is True, doc["probes"]
