"""``ossctl egress status|mode`` with the fourth mode ``sandbox_open`` (open-discovery design §12.2)."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from ossbot.common.control_flags import EGRESS_MODES
from ossbot.ctl import cli


def _api(monkeypatch: pytest.MonkeyPatch, answer: tuple[int, Any]) -> list[tuple[str, str, Any]]:
    sent: list[tuple[str, str, Any]] = []

    def api(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30.0) -> tuple[int, Any]:
        sent.append((method, path, body))
        return answer

    monkeypatch.setattr(cli, "api", api)
    return sent


def test_status_renders_a_sandbox_open_view(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _api(monkeypatch, (200, {"mode": "sandbox_open", "default": "open", "previous": None}))
    result = CliRunner().invoke(cli.app, ["egress", "status"])
    assert result.exit_code == 0, result.output
    assert sent == [("GET", "/v1/control/egress-mode", None)]
    assert "sandbox_open" in result.output and "open" in result.output


def test_mode_sandbox_open_needs_the_tty_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    view = {"mode": "sandbox_open", "default": "open", "previous": "open"}
    sent = _api(monkeypatch, (200, view))
    refused = CliRunner().invoke(cli.app, ["egress", "mode", "sandbox_open"])  # CliRunner's stdin is not a TTY
    assert refused.exit_code == 2 and "interactive TTY confirm" in refused.output and sent == []
    words: list[str] = []
    monkeypatch.setattr(cli, "_confirm", words.append)
    ok = CliRunner().invoke(cli.app, ["egress", "mode", "sandbox_open"])
    assert ok.exit_code == 0, ok.output
    assert words == ["EGRESS SANDBOX_OPEN"]
    assert sent == [("POST", "/v1/control/egress-mode", {"mode": "sandbox_open"})]


def test_unknown_mode_lists_all_four(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _api(monkeypatch, (200, {}))
    monkeypatch.setattr(cli, "_confirm", lambda word: None)
    bad = CliRunner().invoke(cli.app, ["egress", "mode", "sandbox-open"])
    assert bad.exit_code == 2 and sent == []
    assert "allowlist|research|open|sandbox_open" in bad.output


def test_help_texts_list_the_four_modes() -> None:
    assert EGRESS_MODES == ("allowlist", "research", "open", "sandbox_open")
    env = {"COLUMNS": "200", "TERMINAL_WIDTH": "200"}
    group = CliRunner().invoke(cli.app, ["egress", "--help"], env=env)
    mode = CliRunner().invoke(cli.app, ["egress", "mode", "--help"], env=env)
    assert group.exit_code == 0 and mode.exit_code == 0
    assert "allowlist | research | open | sandbox_open" in mode.output
    assert "sandbox_open also gives fetch/validate web_public" in " ".join(mode.output.split())
    top = CliRunner().invoke(cli.app, ["--help"], env=env)
    assert "egress mode (allowlist | research | open | sandbox_open)" in top.output
