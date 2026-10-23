"""The corpus recorded from the pinned Docker CLI (DESIGN §23 item 15, ``record_corpus.py``):

* the CLI model (``sandboxd.runtime.cli_model``) produces exactly the bodies the real CLI sends
  for every sandboxd argv variant (Env compared as a multiset: the CLI does not keep the order);
* the filter allows every recorded create and every recorded follow-up/other operation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ossbot.dockerfilter import rules
from tests.sandboxharness import FakeLookup, create_body, policy, variants

RECORDED = json.loads((Path(__file__).parent / "corpus" / "recorded.json").read_text(encoding="utf-8"))
FAKE_ID = "f" * 64
PROFILE_OF = {
    "ossbot-r1-plan": "research", "ossbot-r1-plan-exec": "agent-exec", "ossbot-r1-plan-stagein": "stage-in",
}  # fmt: skip


def _normalized(body: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(body))
    out["Env"] = sorted(out.get("Env") or [])
    return out


def test_recorded_from_the_pinned_cli() -> None:
    assert RECORDED["docker_cli_version"] == "29.9.1"
    assert set(RECORDED["creates"]) == set(variants())


@pytest.mark.parametrize("name", sorted(variants()))
def test_cli_model_equals_the_real_cli(name: str) -> None:
    assert _normalized(create_body(variants()[name])) == _normalized(RECORDED["creates"][name]["body"])


@pytest.mark.parametrize("name", sorted(variants()))
def test_recorded_create_allowed(name: str, tmp_path: Path) -> None:
    entry = RECORDED["creates"][name]
    req = rules.Request("POST", "/v1.52/containers/create", entry["query"], json.dumps(entry["body"]).encode())
    decision = rules.decide(req, FakeLookup(), policy(tmp_path))
    assert decision.allow, decision.reason


def _lookup() -> FakeLookup:
    lookup = FakeLookup()
    for name, profile in PROFILE_OF.items():
        status = "created" if profile == "stage-in" else "exited"
        lookup.add(name, profile, status, name)
    lookup.add(FAKE_ID, "research", "exited", "ossbot-r1-plan")
    lookup.add("proxy-id", None, "running", "ossbot-proxy")
    lookup.networks["ossbot-t-c-01abcdefgh-research"] = rules.NetworkFacts(
        id="ossbot-t-c-01abcdefgh-research", name="ossbot-t-c-01abcdefgh-research"
    )
    return lookup


OPERATIONS = [(op, i, r) for op, reqs in sorted(RECORDED["operations"].items()) for i, r in enumerate(reqs)]


@pytest.mark.parametrize(("op", "index", "entry"), OPERATIONS, ids=[f"{o}-{i}" for o, i, _ in OPERATIONS])
def test_recorded_operation_allowed(op: str, index: int, entry: dict[str, Any], tmp_path: Path) -> None:
    body = json.dumps(entry["body"]).encode() if "body" in entry else b""
    req = rules.Request(entry["method"], entry["path"], entry["query"], body)
    decision = rules.decide(req, _lookup(), policy(tmp_path))
    assert decision.allow, (op, decision.reason)


def test_networked_agent_exec_is_recorded_and_no_sandbox_container_is_connected() -> None:
    """ADR-0024: the recorded networked agent-exec create is on its own -exec network; networks/connect is
    only ever recorded for the proxy container."""
    body = RECORDED["creates"]["agent-exec-web"]["body"]
    assert body["HostConfig"]["NetworkMode"] == "ossbot-t-c-01abcdefgh-exec"
    assert "OSSBOT_EXEC_NETWORK=web" in body["Env"] and not [e for e in body["Env"] if e.startswith("KIRO_API_KEY")]
    assert RECORDED["operations"]["network_create_exec"], "the -exec network create is recorded"
    connects = [r for _, reqs in RECORDED["operations"].items() for r in reqs if r["path"].endswith("/connect")]
    assert connects and all(r["body"]["Container"] == "ossbot-proxy" for r in connects)


def test_followups_of_docker_run_allowed(tmp_path: Path) -> None:
    """docker run --detach sends wait (no condition) before start, both by container id."""
    for name, entry in RECORDED["creates"].items():
        for line in entry["followed_by"]:
            method, path = line.split(" ", 1)
            decision = rules.decide(rules.Request(method, "/v1.52/" + path, {}, b""), _lookup(), policy(tmp_path))
            assert decision.allow, (name, line, decision.reason)
