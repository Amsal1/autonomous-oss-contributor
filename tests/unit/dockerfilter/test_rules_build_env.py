"""Docker filter regression tests for ADR-0025 (build-env design §12.1, AC 4): the design needs no new container or
network shape, so the filter rules are unchanged; these pin the refusals the floors rely on (internal networks
without a gateway, no network options or IPv6, no sysctls or NET_ADMIN, the stub-DNS ``Dns`` field, no toolchain
cache volume, no task or snapshot volume on key-holding containers, no -exec network on agent-llm) and that the
new environment values (``NO_PROXY``, ``OSSBOT_NET``) are allowed."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from ossbot.dockerfilter import rules
from tests.sandboxharness import FakeLookup, body_request, create_body, policy, set_path, variants
from tests.unit.dockerfilter.test_rules import ALLOW, _lookup, _networked, _request

pytestmark = pytest.mark.unit
EXEC_NET = "ossbot-t-c-01abcdefgh-exec"
TASK = "ossbot-task-c_01abcdefgh"


def _network(**changes: Any) -> rules.Request:
    entry = copy.deepcopy(next(e for e in ALLOW["requests"] if e["name"] == "network-create"))
    for path, value in changes.items():
        set_path(entry["body"], path, value)
    return _request(entry)


def _decide(req: rules.Request, lookup: FakeLookup | None = None, tmp_path: Path | None = None) -> rules.Decision:
    assert tmp_path is not None
    return rules.decide(req, lookup or _lookup(), policy(tmp_path))


def test_the_recorded_network_shape_is_allowed(tmp_path: Path) -> None:
    assert _decide(_network(), tmp_path=tmp_path).allow


@pytest.mark.parametrize("changes", [
    {"Internal": False},  # a routed network: a default route out of the sandbox
    {"IPAM.Config": [{"Subnet": "10.213.0.32/28", "Gateway": "10.213.0.33"}]},
    {"Options": {"com.docker.network.bridge.enable_ip_masquerade": "true"}},
    {"EnableIPv6": True},
    {"IPAM.Config": [{"Subnet": "10.213.0.32/28"}, {"Subnet": "fd00::/64"}]},
    {"Driver": "macvlan"},
])  # fmt: skip
def test_network_shapes_the_floors_rely_on_are_refused(changes: dict[str, Any], tmp_path: Path) -> None:
    decision = _decide(_network(**changes), tmp_path=tmp_path)
    assert not decision.allow, changes


@pytest.mark.parametrize("variant", ["fetch", "validate", "agent-exec-web"])
@pytest.mark.parametrize(("path", "value"), [
    ("HostConfig.Sysctls", {"net.ipv4.ip_unprivileged_port_start": "0"}),
    ("HostConfig.Sysctls", {"net.ipv4.ip_forward": "1"}),
    ("HostConfig.CapAdd", ["NET_ADMIN"]),
    ("HostConfig.CapAdd", ["NET_RAW"]),
    ("HostConfig.Dns", ["8.8.8.8"]),
    ("HostConfig.Dns", ["127.0.0.11"]),
    ("HostConfig.Dns", ["127.0.0.1", "10.255.255.254"]),
    ("HostConfig.NetworkMode", "host"),
    ("HostConfig.NetworkMode", "bridge"),
    ("HostConfig.NetworkMode", "ossbot-egress"),
])  # fmt: skip
def test_repo_code_container_shapes_are_refused(variant: str, path: str, value: Any, tmp_path: Path) -> None:
    body = _networked(variant, EXEC_NET) if variant == "agent-exec-web" else copy.deepcopy(
        create_body(variants()[variant]))  # fmt: skip
    set_path(body, path, value)
    assert not _decide(body_request(body), tmp_path=tmp_path).allow, (variant, path, value)


@pytest.mark.parametrize("variant", ["fetch", "validate", "agent-exec-web"])
def test_a_toolchain_cache_volume_is_refused(variant: str, tmp_path: Path) -> None:
    """§6.6: no shared cache volume (cross-contribution poisoning); toolchains live in the task volume."""
    body = _networked(variant, EXEC_NET) if variant == "agent-exec-web" else copy.deepcopy(
        create_body(variants()[variant]))  # fmt: skip
    mounts = body["HostConfig"]["Mounts"]
    body["HostConfig"]["Mounts"] = [*mounts, {"Type": "volume", "Source": "ossbot-tc-jdk-25", "Target": "/opt/tc",
                                              "ReadOnly": True}]  # fmt: skip
    assert not _decide(body_request(body), tmp_path=tmp_path).allow


@pytest.mark.parametrize("variant", ["agent-llm", "research-none", "research-snapshot", "catalog", "probe-agent-llm"])
@pytest.mark.parametrize(("source", "target", "read_only"), [
    (TASK, "/work", False), (TASK, "/work", True), (TASK, "/mnt/task", True),
    ("ossbot-ro-r1-plan", "/work/repo", True), ("ossbot-tc-jdk-25", "/opt/tc", True),
])  # fmt: skip
def test_key_holders_get_no_task_snapshot_or_cache_mount(variant: str, source: str, target: str, read_only: bool,
                                                         tmp_path: Path) -> None:  # fmt: skip
    body = copy.deepcopy(create_body(variants()[variant]))
    mounts = [m for m in body["HostConfig"]["Mounts"] if m["Target"] != target]
    if variant.startswith("research") and source == "ossbot-ro-r1-plan":
        read_only = False  # research's own run snapshot is the ADR-0017 shape, and only read-only
    body["HostConfig"]["Mounts"] = [*mounts, {"Type": "volume", "Source": source, "Target": target,
                                              **({"ReadOnly": True} if read_only else {})}]  # fmt: skip
    assert not _decide(body_request(body), tmp_path=tmp_path).allow, (variant, source, target, read_only)


@pytest.mark.parametrize("network", [EXEC_NET, "ossbot-t-c-01abcdefgh-fetch", "ossbot-t-c-01abcdefgh-validate"])
def test_agent_llm_never_on_a_repo_code_network(network: str, tmp_path: Path) -> None:
    assert not _decide(body_request(_networked("agent-llm", network)), tmp_path=tmp_path).allow


@pytest.mark.parametrize("variant", ["fetch", "validate", "probe-fetch"])
@pytest.mark.parametrize("net", ["web", "base"])
def test_the_new_env_values_are_allowed(variant: str, net: str, tmp_path: Path) -> None:
    body = copy.deepcopy(create_body(variants()[variant]))
    assert "NO_PROXY=localhost,127.0.0.1,::1,.localhost" in body["Env"]
    body["Env"] = [*body["Env"], f"OSSBOT_NET={net}"]
    decision = _decide(body_request(body), tmp_path=tmp_path)
    assert decision.allow, decision.reason


def test_the_networked_agent_exec_keeps_its_recorded_shape(tmp_path: Path) -> None:
    body = _networked("agent-exec-web", EXEC_NET)
    assert "NO_PROXY=localhost,127.0.0.1,::1,.localhost" in body["Env"] and "OSSBOT_EXEC_NETWORK=web" in body["Env"]
    assert _decide(body_request(body), tmp_path=tmp_path).allow
    assert json.dumps(body["HostConfig"].get("CapAdd")) in ("null", "[]")
