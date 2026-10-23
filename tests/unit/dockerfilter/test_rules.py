"""Docker API filter rules (DESIGN §10.8, §22.1): allow corpus, deny corpus, kill/pause (N5)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from ossbot.common.images import load_image_pins
from ossbot.dockerfilter import rules
from tests.conftest import write_flag
from tests.sandboxharness import (
    DELETE,
    ENVIRON,
    FakeLookup,
    body_request,
    create_body,
    create_request,
    policy,
    set_path,
    variants,
)

CORPUS = Path(__file__).parent / "corpus"
ALLOW = json.loads((CORPUS / "allow.json").read_text(encoding="utf-8"))
DENY = json.loads((CORPUS / "deny.json").read_text(encoding="utf-8"))


def _lookup(extra: dict[str, Any] | None = None) -> FakeLookup:
    lookup = FakeLookup()
    for cid, c in ALLOW["lookup"]["containers"].items():
        lookup.add(cid, c["profile"], c["status"], c.get("name"))
    lookup.add("zzz999", None, "running", "unrelated")
    lookup.add("eee555", "stage-in", "running")
    lookup.add("fff666", "research", "running")
    for nid, name in {**ALLOW["lookup"]["networks"], "ossbot-egress": "ossbot-egress", "bridge": "bridge"}.items():
        lookup.networks[nid] = rules.NetworkFacts(id=nid, name=name)
    return lookup


def _request(entry: dict[str, Any]) -> rules.Request:
    if "raw_body" in entry:
        body = entry["raw_body"].encode()
    elif "body" in entry:
        body = json.dumps(entry["body"]).encode()
    else:
        body = b""
    return rules.Request(entry["method"], entry["path"], entry.get("query", {}), body)


# ------------------------------------------------------------------------------ allow
@pytest.mark.parametrize("name", sorted(variants()))
def test_every_argv_variant_create_is_allowed(name: str, tmp_path: Path) -> None:
    decision = rules.decide(create_request(variants()[name]), _lookup(), policy(tmp_path))
    assert decision.allow, decision.reason
    assert decision.operation == "containers/create"


@pytest.mark.parametrize("entry", ALLOW["requests"], ids=lambda e: e["name"])
def test_allow_corpus(entry: dict[str, Any], tmp_path: Path) -> None:
    decision = rules.decide(_request(entry), _lookup(), policy(tmp_path))
    assert decision.allow, decision.reason


def test_api_version_prefix_any(tmp_path: Path) -> None:
    for prefix in ("", "/v1.41", "/v1.52", "/v1.99"):
        assert rules.decide(rules.Request("GET", f"{prefix}/version"), _lookup(), policy(tmp_path)).allow


# ------------------------------------------------------------------------------- deny
def _mutate(entry: dict[str, Any]) -> rules.Request:
    body = copy.deepcopy(create_body(variants()[entry["base"]]))
    for path, value in entry.get("set", {}).items():
        set_path(body, path, value)
    for key, value in entry.get("labels", {}).items():
        body["Labels"][key] = value
    for key in entry.get("labels_delete", []):
        body["Labels"].pop(key)
    for item in entry.get("env_add", []):
        body["Env"].append(item.replace("LONG4097", "x" * 4097))
    for item in entry.get("env_add_if_missing", []):
        if item.split("=")[0] not in {e.split("=")[0] for e in body["Env"]}:
            body["Env"].append(item)
    if "cmd" in entry:
        body["Cmd"] = entry["cmd"]
    return body_request(body)


@pytest.mark.parametrize("entry", DENY["create"], ids=lambda e: e["name"])
def test_deny_corpus_create_mutations(entry: dict[str, Any], tmp_path: Path) -> None:
    decision = rules.decide(_mutate(entry), _lookup(), policy(tmp_path))
    assert not decision.allow, entry["name"]


@pytest.mark.parametrize("entry", DENY["request"], ids=lambda e: e["name"])
def test_deny_corpus_requests(entry: dict[str, Any], tmp_path: Path) -> None:
    decision = rules.decide(_request(entry), _lookup(), policy(tmp_path))
    assert not decision.allow, entry["name"]
    assert decision.status == 403


def test_unknown_nonzero_key_reason(tmp_path: Path) -> None:
    body = create_body(variants()["fetch"])
    body["HostConfig"]["OomScoreAdj"] = -1000
    decision = rules.decide(body_request(body), _lookup(), policy(tmp_path))
    assert decision.reason == "unknown_key:HostConfig.OomScoreAdj"


def test_unknown_zero_keys_are_tolerated(tmp_path: Path) -> None:
    body = create_body(variants()["fetch"])
    body["HostConfig"]["SomeFutureKey"] = None
    body["AnotherFutureKey"] = []
    body["HostConfig"]["YetAnother"] = {}
    assert rules.decide(body_request(body), _lookup(), policy(tmp_path)).allow


def test_duplicate_json_key_rejected(tmp_path: Path) -> None:
    raw = create_request(variants()["fetch"]).body
    # Inject a second "Privileged" key after the first one: last-wins parsers would see true.
    doctored = raw.replace(b'"Privileged":false', b'"Privileged":false,"Privileged":true', 1)
    assert doctored != raw
    decision = rules.decide(body_request(doctored), _lookup(), policy(tmp_path))
    assert decision.reason == "duplicate_key:Privileged"


def test_nan_constant_rejected(tmp_path: Path) -> None:
    raw = create_request(variants()["fetch"]).body.replace(b'"NanoCpus":2000000000', b'"NanoCpus":NaN', 1)
    assert not rules.decide(body_request(raw), _lookup(), policy(tmp_path)).allow


def test_container_name_must_be_ossbot(tmp_path: Path) -> None:
    body = create_body(variants()["fetch"])
    assert not rules.decide(body_request(body, name="evil"), _lookup(), policy(tmp_path)).allow


def test_key_only_for_key_pairs(tmp_path: Path) -> None:
    for name, spec in variants().items():
        env = {e.split("=")[0] for e in create_body(spec)["Env"]}
        expected = (spec.profile, spec.entrypoint) in rules.KEY_PAIRS
        assert ("KIRO_API_KEY" in env) is expected, name


@pytest.mark.parametrize(
    ("variant", "role", "allowed"),
    [("research-scout", "scout", True), ("research-scout", "reviewer", True), ("research-scout", "planner", False),
     ("agent-llm", "scout", False), ("fetch", None, False), ("validate", None, False)],
)  # fmt: skip
def test_read_token_env_only_for_research_kiro_mcp_roles(
    tmp_path: Path, variant: str, role: str | None, allowed: bool
) -> None:
    """GHMCP-2, second layer: the filter refuses GITHUB_READ_TOKEN outside research/kiro scout|reviewer."""
    body = create_body(variants()[variant])
    env = [e for e in body["Env"] if not e.startswith(("GITHUB_READ_TOKEN=", "ROLE="))]
    env.append("GITHUB_READ_TOKEN=github_pat_" + "T" * 30)
    if role is not None:
        env.append(f"ROLE={role}")
    body["Env"] = env
    decision = rules.decide(body_request(body), _lookup(), policy(tmp_path))
    assert decision.allow is allowed, decision.reason
    if not allowed:
        assert decision.reason.startswith("env_read_token_not_allowed")


# ------------------------------------------------- ADR-0024: the networked agent-exec shape
EXEC_NET = "ossbot-t-c-01abcdefgh-exec"


def _networked(base: str, network: str, dns: bool = True) -> dict[str, Any]:
    body = copy.deepcopy(create_body(variants()[base]))
    set_path(body, "HostConfig.NetworkMode", network)
    if dns:
        set_path(body, "HostConfig.Dns", ["127.0.0.1"])
        set_path(body, "HostConfig.DnsSearch", ["."])
    else:
        set_path(body, "HostConfig.Dns", DELETE)
        set_path(body, "HostConfig.DnsSearch", DELETE)
    endpoints = body.get("NetworkingConfig", {}).get("EndpointsConfig")
    if endpoints:
        body["NetworkingConfig"]["EndpointsConfig"] = {network: next(iter(endpoints.values()))}
    return body


@pytest.mark.parametrize(
    ("base", "network", "dns", "allowed"),
    [
        ("agent-exec-web", EXEC_NET, True, True),
        ("agent-exec-web", "ossbot-t-c-01abcdefgj-exec", True, False),  # another cid's -exec
        ("agent-exec-web", "ossbot-t-c-01abcdefgh-llm", True, False),
        ("agent-exec-web", "ossbot-t-c-01abcdefgh-fetch", True, False),
        ("agent-exec-web", "ossbot-t-c-01abcdefgh-validate-strict", True, False),
        ("agent-exec-web", "ossbot-t-c-01abcdefgh-research", True, False),
        ("agent-exec-web", EXEC_NET, False, False),  # networked without the DNS fields
        ("agent-exec", "none", False, True),
        ("agent-llm", "ossbot-t-c-01abcdefgh-llm", True, True),
        ("agent-llm", EXEC_NET, True, False),
        ("agent-llm", "ossbot-t-c-01abcdefgj-llm", True, False),  # another cid's -llm
        ("probe-agent-llm", EXEC_NET, True, False),
    ],
)
def test_coder_pair_network_binding(base: str, network: str, dns: bool, allowed: bool, tmp_path: Path) -> None:
    body = _networked(base, network, dns) if network != "none" else create_body(variants()[base])
    decision = rules.decide(body_request(body), _lookup(), policy(tmp_path))
    assert decision.allow is allowed, decision.reason


@pytest.mark.parametrize("name", ["KIRO_API_KEY", "GITHUB_READ_TOKEN"])
def test_networked_agent_exec_never_gets_a_credential(name: str, tmp_path: Path) -> None:
    body = _networked("agent-exec-web", EXEC_NET)
    body["Env"] = [*body["Env"], f"{name}={ENVIRON[name]}"]
    assert not rules.decide(body_request(body), _lookup(), policy(tmp_path)).allow


def _network_create(name: str, subnet: str) -> rules.Request:
    entry = copy.deepcopy(next(e for e in ALLOW["requests"] if e["name"] == "network-create"))
    entry["body"]["Name"] = name
    entry["body"]["IPAM"]["Config"][0]["Subnet"] = subnet
    entry["body"]["Labels"]["ossbot.profile"] = name.rsplit("-", 1)[1]
    return _request(entry)


@pytest.mark.parametrize(
    ("name", "subnet", "allowed"),
    [
        (EXEC_NET, "10.213.0.32/28", True),
        (EXEC_NET, "10.213.63.240/28", True),
        (EXEC_NET, "10.213.64.0/28", False),  # research pool
        (EXEC_NET, "10.213.128.0/28", False),  # llm pool
        (EXEC_NET, "10.213.192.0/28", False),  # pool_strict
        ("ossbot-t-c-01abcdefgh-research", "10.213.64.0/28", True),  # other networks unchanged
    ],
)
def test_exec_networks_only_in_pool_fetch(name: str, subnet: str, allowed: bool, tmp_path: Path) -> None:
    decision = rules.decide(_network_create(name, subnet), _lookup(), policy(tmp_path))
    assert decision.allow is allowed, decision.reason


def _connect(container: str, aliases: list[str] | None = None, op: str = "connect") -> rules.Request:
    body: dict[str, Any] = {"Container": container}
    if op == "connect" and aliases is not None:
        body["EndpointConfig"] = {"Aliases": aliases}
    if op == "disconnect":
        body["Force"] = True
    return rules.Request("POST", f"/v1.52/networks/ossbot-t-c-01abcdefgh-research/{op}", {},
                         json.dumps(body).encode())  # fmt: skip


@pytest.mark.parametrize(
    ("container", "aliases", "reason"),
    [
        ("ossbot-proxy", ["proxy"], None),
        ("ossbot-proxy", None, None),
        ("ossbot-proxy", ["other"], "connect_aliases"),
        ("xe0001", None, "connect_container_sandbox"),  # agent-exec
        ("xl0001", None, "connect_container_sandbox"),  # agent-llm
        ("ccc333", None, "connect_container_sandbox"),  # fetch
        ("aaa111", ["proxy"], "connect_container_sandbox"),
    ],
)
def test_connect_is_proxy_only(container: str, aliases: list[str] | None, reason: str | None, tmp_path: Path) -> None:
    lookup = _lookup()
    lookup.add("xe0001", "agent-exec", "running")
    lookup.add("xl0001", "agent-llm", "running")
    decision = rules.decide(_connect(container, aliases), lookup, policy(tmp_path))
    if reason is None:
        assert decision.allow, decision.reason
    else:
        assert not decision.allow and reason in decision.reason


@pytest.mark.parametrize("container", ["ossbot-proxy", "ccc333"])
def test_disconnect_unchanged(container: str, tmp_path: Path) -> None:
    assert rules.decide(_connect(container, op="disconnect"), _lookup(), policy(tmp_path)).allow


# -------------------------------------------------------------------- kill / pause (N5)
@pytest.mark.parametrize("flag", ["killed", "paused"])
def test_create_refused_while_flagged(flag: str, tmp_path: Path) -> None:
    pol = policy(tmp_path)
    write_flag(pol.control_dir / flag, {"level": "soft", "reason": "test", "actor": "t", "ts": "x"})
    decision = rules.decide(create_request(variants()["fetch"]), _lookup(), pol)
    assert not decision.allow
    assert decision.reason == f"{flag}:docker_filter:containers/create"


@pytest.mark.parametrize("flag", ["killed", "paused"])
def test_start_refused_while_flagged(flag: str, tmp_path: Path) -> None:
    pol = policy(tmp_path)
    write_flag(pol.control_dir / flag, "not json")  # unparseable still counts (fail closed)
    decision = rules.decide(rules.Request("POST", "/v1.52/containers/ccc333/start"), _lookup(), pol)
    assert not decision.allow
    assert decision.reason.startswith(flag)


@pytest.mark.parametrize("op", ["wait", "kill", "inspect"])
def test_teardown_still_allowed_while_killed(op: str, tmp_path: Path) -> None:
    pol = policy(tmp_path)
    write_flag(pol.control_dir / "killed", {"level": "full"})
    method, suffix = {"wait": ("POST", "/wait"), "kill": ("POST", "/kill"), "inspect": ("GET", "/json")}[op]
    assert rules.decide(rules.Request(method, f"/v1.52/containers/aaa111{suffix}"), _lookup(), pol).allow


# --------------------------------------------------------------------- image allowlist
def test_image_allowlist_from_lock_copy(tmp_path: Path) -> None:
    lock = tmp_path / "versions.lock.yaml"
    lock.write_text(
        'images:\n  sandbox_full:\n    digest: "sha256:' + "a" * 64 + '"\n'
        '  sandbox_llm:\n    digest: "pending (FEAT-004 images step)"\n',
        encoding="utf-8",
    )
    assert rules.load_image_allowlist(lock) == {"sandbox-full": frozenset({"a" * 64})}
    assert rules.load_image_allowlist(tmp_path / "missing.yaml") == {}


def test_repo_lock_images_are_recorded_by_the_images_step() -> None:
    """FEAT-004's provision step 'images' wrote every digest and probe hash (until then: pending)."""
    pins = load_image_pins(Path(__file__).resolve().parents[3] / "config" / "versions.lock.yaml")
    assert set(pins.digests) == {"sandbox-full", "sandbox-llm", "proxy"}
    assert pins.agent_configs_sha256 is not None and pins.settings_sha256 is not None
    assert pins.kiro_cli_version == "2.33.1"


def test_empty_allowlist_refuses_every_create(tmp_path: Path) -> None:
    pol = rules.FilterPolicy({}, policy(tmp_path).control_dir)
    for spec in variants().values():
        assert rules.decide(create_request(spec), _lookup(), pol).reason == "image_digest"


def test_route_table_has_no_exec_or_build() -> None:
    ops = {op for _, _, op in rules._ROUTES}
    assert not {o for o in ops if "exec" in o or "build" in o or "images" in o or "attach" in o}


def test_delete_marker_helper() -> None:
    doc: dict[str, Any] = {"a": {"b": 1}}
    set_path(doc, "a.b", DELETE)
    assert doc == {"a": {}}
