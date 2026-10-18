"""DESIGN §22.5 sandbox isolation smoke tests, one container per profile via sandboxd's templates.

Every bullet that needs no Kiro key runs here; the Kiro host CONNECT checks and S-ws-1 (a real
coder/planner run over the hostile fixture) are ``needs_key`` and skipped with the reason.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

import pytest

from tests.sandbox.conftest import Sandbox, proxy_ip_on

Pool = Literal["fetch", "research", "llm"]
SQUID_CONF = Path("/etc/ossbot/proxy/squid.conf")

CID = "c_SMOKETEST1"
PEER_CID = "c_SMOKETEST2"
SNAP_CID = "c_SMOKETEST3"
FETCH_CID = "c_SMOKETEST4"
needs_key = pytest.mark.skip(reason="needs_key: requires the sandbox KIRO_API_KEY (live probe, DESIGN §22.7)")
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
    match = re.search(r"^# Egress mode: (allowlist|research|open) ", SQUID_CONF.read_text(encoding="utf-8"), re.M)
    assert match, f"{SQUID_CONF} names no egress mode (redeploy)"
    return match.group(1)


def expected_egress(pool: Pool, mode: str) -> tuple[bool, bool]:
    """(web_public, github_user_content) of a source pool in an egress mode (DESIGN §11.3, §11.5):
    research has github_user_content in every mode and web_public outside allowlist; the llm pool
    (agent-llm, catalog) has web_public (which covers github.io and gists) only in open; fetch and
    validate never get either."""
    if pool == "research":
        return mode != "allowlist", True
    if pool == "llm":
        return mode == "open", mode == "open"
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
        # no github_read for the llm pool; github.com only through web_public in egress mode open
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


@needs_key
def test_kiro_host_connect() -> None:
    """research / agent-llm: Kiro host CONNECT ok (needs the key to be meaningful)."""


@needs_key
def test_workspace_canary_s_ws_1() -> None:
    """S-ws-1: the fixture's .kiro/settings/mcp.json and hooks never run during a coder or planner run."""
