"""Kiro V3 ACP contract (DESIGN §15.2, §22.3, AC 20) against the scripted fake server."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.acp.conftest import FakeRun, ask, client, v3, v3_core


def test_launch_argv_is_v3_without_v2_flags() -> None:
    for auth in v3_core.AUTH_METHODS:
        spec = v3_core.launch_spec(auth_method=auth, api_key="ksk_" + "x" * 20)
        assert spec.argv[:3] == ("/home/hermes/.local/bin/kiro-cli", "acp", "--agent-engine=v3")
        for flag in v3_core.V2_ONLY_FLAGS:
            assert not any(a == flag or a.startswith(flag + "=") for a in spec.argv)
        assert spec.cwd == "/home/hermes/.hermes/kiro-cwd"
        assert spec.env["HTTPS_PROXY"] == "http://10.212.0.2:3141"
        assert ("KIRO_API_KEY" in spec.env) is (auth == "default")  # option (a) only
        assert ("--auth-method=client" in spec.argv) is (auth == "client")
    with pytest.raises(ValueError):
        v3_core.launch_spec(kiro_cli="kiro-cli")  # full executable path only


def test_initialize_offers_no_client_capabilities() -> None:
    params = v3_core.initialize_params()
    assert params["clientCapabilities"] == {}  # no fs/* or terminal/* bridge
    assert params["protocolVersion"] == 1


def test_session_new_carries_cwd_mcp_and_meta(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    run = fake()
    c = client(run, tmp_path)
    out = ask(c)
    assert out.choices[0].message.content == "ok"
    new = run.requests("session/new")[0]["params"]
    assert new["cwd"] == "/home/hermes/.hermes/workspace"
    assert new["mcpServers"] == []
    assert new["_meta"]["kiro"] == {"modeId": "ossbot-chat", "modelId": "claude-sonnet-5.5", "effortLevel": "medium"}
    init = run.requests("initialize")[0]["params"]
    assert init["clientCapabilities"] == {}
    c.close()


def test_optional_methods_never_called_unless_advertised(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    run = fake(init={"protocolVersion": 1, "agentCapabilities": {"loadSession": True}})
    c = client(run, tmp_path)
    ask(c)
    allowed = {"initialize", "session/new", "session/set_config_option", "session/prompt"}
    assert set(run.methods()) <= allowed
    assert not [m for m in run.methods() if m.startswith("_kiro/")]
    c.close()


def test_turn_completes_on_the_prompt_response_with_chunks(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    run = fake(
        prompts=[
            [
                {"thought": "thinking"},
                {"text": "Hello "},
                {"text": "world"},
                {
                    "raw": '{"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "sess-1",'
                    ' "update": {"sessionUpdate": "session_info_update", "_meta": {"kiro": {"kind": "turn_end"}}}}}'
                },
                {"text": "!"},
            ]
        ]
    )
    c = client(run, tmp_path)
    out = ask(c)
    assert out.choices[0].message.content == "Hello world!"  # text after turn_end still belongs to the turn
    assert out.choices[0].message.reasoning == "thinking"
    c.close()


def test_unknown_agent_request_gets_method_not_found(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    run = fake(
        prompts=[
            [
                {"request": "fs/read_text_file"},
                {"request": "terminal/create"},
                {"request": "_kiro/openExternalUrl"},
                {"text": "done"},
            ]
        ]
    )
    c = client(run, tmp_path)
    assert ask(c).choices[0].message.content == "done"
    errors = [f for f in run.responses() if "error" in f]
    assert len(errors) == 3
    assert all(e["error"]["code"] == -32601 for e in errors)
    c.close()


def test_cancel_sends_session_cancel(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    run = fake(prompts=[[{"text": "partial"}, {"wait_cancel": True}]])
    c = client(run, tmp_path)
    import threading

    box: dict[str, object] = {}

    def turn() -> None:
        try:
            box["out"] = ask(c)
        except Exception as exc:  # noqa: BLE001 - the turn's outcome is recorded, not asserted
            box["err"] = exc

    t = threading.Thread(target=turn)
    t.start()
    for _ in range(100):
        if c._active is not None:
            break
        threading.Event().wait(0.05)
    c.close()
    t.join(timeout=10)
    assert "session/cancel" in run.methods()


def test_transport_drop_reloads_without_resending_the_prompt(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    run = fake(prompts=[[{"text": "par"}, {"drop": True}], [{"text": "next"}]])
    c = client(run, tmp_path)
    with pytest.raises(v3.TurnLost):
        ask(c)
    methods = run.methods()
    assert methods.count("session/prompt") == 1  # never resent
    load = run.requests("session/load")
    assert len(load) == 1
    assert load[0]["params"] == {"sessionId": "sess-1", "cwd": "/home/hermes/.hermes/workspace", "mcpServers": []}
    assert methods.index("session/load") > methods.index("session/prompt")
    # the reconnected process registered its handlers before loading: initialize came first
    assert methods[methods.index("session/prompt") + 1] == "initialize"
    assert ask(c).choices[0].message.content == "next"
    c.close()


def test_malformed_lines_close_the_transport_after_three(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    run = fake(prompts=[[{"raw": "not json"}, {"raw": "[1]"}, {"raw": "{}"}, {"sleep": 1}, {"text": "late"}]])
    c = client(run, tmp_path)
    with pytest.raises(v3.TurnLost):
        ask(c)
    c.close()


def test_models_source_follows_role_indirection(tmp_path: Path) -> None:
    import json

    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            {
                "default": {"model": "a", "effort": "high"},
                "roles": {
                    "chat": {"model": "planner", "effort": "low"},
                    "planner": {"model": "default"},
                    "vision": {"model": "v"},
                },
                "fallbacks": ["b"],
            }
        ),
        encoding="utf-8",
    )
    src = v3.ModelsSource(str(path))
    assert src.resolve("chat") == v3.ResolvedModel("a", "low", ("b",))
    assert src.resolve("vision") == v3.ResolvedModel("v", None, ("b",))
    assert src.resolve("missing") == v3.ResolvedModel("a", "high", ("b",))
