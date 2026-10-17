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


def test_repo_lock_images_are_pending_until_feat004() -> None:
    pins = load_image_pins(Path(__file__).resolve().parents[3] / "config" / "versions.lock.yaml")
    assert pins.digests == {}  # fail closed: no image allowed before the images step records digests
    assert pins.kiro_cli_version == "2.32.0"


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
