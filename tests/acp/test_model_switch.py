"""Live chat model switch (DESIGN §15.2, §16, AC 13/20): the models file is read per completion, the
new model reaches the next session without a restart, and only ``session/set_config_option`` is used."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.acp.conftest import FakeRun, ask, client, models_file


def _switch(tmp_path: Path, chat: str) -> None:
    path = tmp_path / "models.json"
    before = path.stat().st_mtime_ns
    models_file(tmp_path, chat=chat)
    if path.stat().st_mtime_ns == before:  # coarse filesystem clocks
        os.utime(path, ns=(before + 1_000_000, before + 1_000_000))


def _sets(run: FakeRun) -> list[tuple[str, str]]:
    return [(f["params"]["configId"], f["params"]["value"]) for f in run.requests("session/set_config_option")]


def test_meta_honoured_switch_needs_no_config_call(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    run = fake(session={"honour_meta_model": True})
    c = client(run, tmp_path)
    ask(c)
    _switch(tmp_path, "claude-opus-5.5")
    ask(c)
    metas = [f["params"]["_meta"]["kiro"]["modelId"] for f in run.requests("session/new")]
    assert metas == ["claude-sonnet-5.5", "claude-opus-5.5"]
    assert _sets(run) == []
    assert c.last_turn.model == "claude-opus-5.5"
    assert run.methods().count("initialize") == 1  # same process: no restart
    assert "session/set_model" not in run.methods()
    c.close()


def test_switch_sends_exactly_one_set_config_option(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    """The fake ignores ``_meta.kiro.modelId`` and keeps every session on its previous model."""
    run = fake(session={"current_model": "claude-sonnet-5.5", "current_effort": "medium"})
    c = client(run, tmp_path)
    ask(c)
    assert _sets(run) == []  # the session already runs the resolved model and effort
    _switch(tmp_path, "claude-opus-5.5")
    time.sleep(0.01)
    ask(c)
    assert _sets(run) == [("model", "claude-opus-5.5")]
    assert c.last_turn.model == "claude-opus-5.5"
    assert run.methods().count("initialize") == 1
    assert "session/set_model" not in run.methods()
    c.close()


YAML_MODELS = """\
version: 1
default: {model: claude-opus-5.5, effort: high}   # a comment
roles:
  chat:       { model: claude-sonnet-5.5, effort: medium }
  vision:     { model: chat }
  test:       { model: claude-haiku-4.5, effort: low, fallback: none }
fallbacks: [claude-opus-5.5, claude-sonnet-5.5]
"""


def test_the_real_yaml_models_file_is_read_without_pyyaml(
    fake: Callable[..., FakeRun], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hermes v0.22.2 ships ruamel.yaml and no PyYAML (found live in item 38): config/models.yaml must load."""
    monkeypatch.setitem(sys.modules, "yaml", None)  # import yaml -> ImportError
    path = tmp_path / "models.yaml"
    path.write_text(YAML_MODELS, encoding="utf-8")
    run = fake(session={"honour_meta_model": True})
    c = client(run, tmp_path, models=path, request_roles=frozenset({"test"}))
    ask(c)
    c.chat.completions.create(model="role:test", messages=[{"role": "user", "content": "hi"}], timeout=20)
    metas = [f["params"]["_meta"]["kiro"] for f in run.requests("session/new")]
    assert [m["modelId"] for m in metas] == ["claude-sonnet-5.5", "claude-haiku-4.5"]
    assert [m["effortLevel"] for m in metas] == ["medium", "low"]
    c.close()


def test_unavailable_model_falls_back_per_models_file(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    run = fake(session={"models": ["claude-opus-5.5"], "current_model": "auto"})
    c = client(run, tmp_path, models=models_file(tmp_path, chat="claude-gone-1"))
    ask(c)
    assert _sets(run) == [("model", "claude-opus-5.5")]
    c.close()


def test_request_role_only_from_the_allowlist(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    """``model="role:test"`` selects roles.test only when ``test`` is a request role; anything else is ignored."""
    path = tmp_path / "models.json"
    path.write_text(json.dumps({
        "default": {"model": "claude-opus-5.5", "effort": "high"},
        "roles": {"chat": {"model": "claude-sonnet-5.5", "effort": "medium"},
                  "test": {"model": "claude-haiku-4.5", "effort": "low"}, "coder": {"model": "claude-opus-5.5"}},
    }), encoding="utf-8")  # fmt: skip
    run = fake(session={"honour_meta_model": True})
    c = client(run, tmp_path, models=path, request_roles=frozenset({"test"}))
    msgs = [{"role": "user", "content": "hello"}]
    for model in ("role:test", "role:coder", "claude-opus-5.5", "kiro-acp"):
        c.chat.completions.create(model=model, messages=msgs, timeout=20)
    metas = [f["params"]["_meta"]["kiro"]["modelId"] for f in run.requests("session/new")]
    assert metas == ["claude-haiku-4.5", "claude-sonnet-5.5", "claude-sonnet-5.5", "claude-sonnet-5.5"]
    assert c.request_role("role:test", True) == "test"  # no test_vision listed: the same role
    c.close()


def test_effort_set_only_when_advertised(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    run = fake(session={"current_model": "claude-sonnet-5.5", "current_effort": "low"})
    c = client(run, tmp_path)
    ask(c)
    assert _sets(run) == [("effortLevel", "medium")]
    c.close()
