"""The Hermes ``ossbot`` plugin (hermes/plugins/ossbot; DESIGN §15.1, §15.6): the code-job tool's
argument checks and polling, and the per-feature SQLite stores. Loaded by path under another package
name (the plugin is copied into Hermes, not installed)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "hermes" / "plugins" / "ossbot"


def _load() -> ModuleType:
    name = "ossbot_hermes_plugin_under_test"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, PLUGIN / "__init__.py",
                                                      submodule_search_locations=[str(PLUGIN)])  # fmt: skip
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return importlib.import_module(f"{name}.tools")


tools = _load()
features = sys.modules["ossbot_hermes_plugin_under_test.features"]


def test_the_code_tool_is_registered_in_the_ossbot_toolset() -> None:
    names = [schema["name"] for schema, _ in tools.TOOLS]
    assert "ossbot_request_sandbox_job" in names and all(n.startswith("ossbot_") for n in names)


@pytest.mark.parametrize(
    "params",
    [
        {"language": "ruby", "code": "x"},
        {"language": "python", "code": ""},
        {"language": "python", "code": "x", "kind": "research"},
        {"language": "python", "code": "x", "files": ["../etc/passwd"]},
        {"language": "python", "code": "x", "files": [f"a_{i:016x}" for i in range(6)]},
        {"language": "python", "code": "x", "timeout_s": 600},
    ],
)
def test_bad_arguments_never_reach_the_gatekeeper(monkeypatch: pytest.MonkeyPatch, params: dict[str, Any]) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(tools, "call", lambda *a, **k: calls.append(a) or (500, None))
    out = json.loads(tools.ossbot_request_sandbox_job(params))
    assert out["success"] is False and calls == []


def test_submits_then_polls_until_finished(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, Any]] = []
    states = iter(["queued", "running", "done"])

    def fake(method: str, path: str, body: Any = None, **_: Any) -> tuple[int, Any]:
        calls.append((method, path, body))
        if method == "POST":
            return 202, {"job_id": "k_0123456789abcdef", "status": "queued"}
        return 200, {"job_id": "k_0123456789abcdef", "status": next(states), "stdout": "4\n"}

    monkeypatch.setattr(tools, "call", fake)
    monkeypatch.setattr(tools.time, "sleep", lambda s: None)
    out = json.loads(tools.ossbot_request_sandbox_job(
        {"language": "python", "code": "print(2 + 2)", "files": ["a_00000000000000aa"], "timeout_s": 30}))  # fmt: skip
    assert out == {"success": True, "data": {"job_id": "k_0123456789abcdef", "status": "done", "stdout": "4\n"}}
    assert calls[0] == ("POST", "/v1/chat/sandbox-jobs", {"kind": "code", "language": "python",
                        "code": "print(2 + 2)", "files": ["a_00000000000000aa"], "timeout_s": 30})  # fmt: skip
    assert [c[1] for c in calls[1:]] == ["/v1/chat/sandbox-jobs/k_0123456789abcdef"] * 3


def test_a_refusal_is_returned_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "call", lambda *a, **k: (403, {"error": "tier"}))
    out = json.loads(tools.ossbot_request_sandbox_job({"language": "bash", "code": "echo"}))
    assert out == {"success": False, "status": 403, "error": {"error": "tier"}}


def test_feature_store_round_trip(tmp_path: Path) -> None:
    store = features.FeatureStore("food_log", base=tmp_path)
    assert store.save("2026-10-19T12:00-lunch", {"protein_g": 31}) == {"feature": "food_log",
                                                                      "key": "2026-10-19T12:00-lunch",
                                                                      "updated": False}  # fmt: skip
    store.save("2026-10-20T08:00-breakfast", {"protein_g": 12})
    assert [r["key"] for r in store.query("2026-10")] == ["2026-10-20T08:00-breakfast", "2026-10-19T12:00-lunch"]
    assert (tmp_path / "food_log.db").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        features.FeatureStore("passwords", base=tmp_path)
    with pytest.raises(ValueError):
        store.save("bad key!", {})
    with pytest.raises(ValueError):
        store.save("k", {"x": "y" * 5000})
    assert store.delete("2026-10-19T12:00-lunch") and not store.delete("2026-10-19T12:00-lunch")


def test_notify_and_feature_cards_carry_the_turns_session_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """U12: unsolicited messages name the turn they serve; the key comes from Hermes's session vars."""
    calls: list[tuple[str, Any, Any]] = []
    monkeypatch.setattr(tools, "call", lambda m, p, b=None, **k: calls.append((p, b, k)) or (202, {"queued": True}))
    assert json.loads(tools.ossbot_notify({"text": "lunch?", "priority": "P1"}))["success"] is True
    assert json.loads(tools.ossbot_feature_card({"feature": "nutrition", "kind": "meal", "data": {"g": 1}}))["success"]
    assert calls == [
        ("/v1/notify", {"text": "lunch?", "priority": "P1"}, {"session": True}),
        ("/v1/features/nutrition/cards", {"kind": "meal", "data": {"g": 1}}, {"session": True}),
    ]
    assert json.loads(tools.ossbot_notify({"text": "x", "priority": "P0"}))["success"] is False
    monkeypatch.setenv("HERMES_SESSION_KEY", "tg-4242-t7")
    assert tools.session_key() == "tg-4242-t7"  # outside Hermes: the environment (no gateway module)
