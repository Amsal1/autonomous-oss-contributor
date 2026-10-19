"""Client auth (DESIGN §15.2 option b): ``_kiro/auth/getAccessToken`` is answered from the token file
and the token never appears in any log record."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.acp.conftest import FakeRun, ask, client

TOKEN = "ksk_" + "Q7" * 20


def test_token_answered_and_never_logged(
    fake: Callable[..., FakeRun], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    token_file = tmp_path / "hermes.env"
    token_file.write_text(f"OTHER=1\nKIRO_API_KEY={TOKEN}\n", encoding="utf-8")
    run = fake(prompts=[[{"token": True}, {"text": "authed"}]])
    caplog.set_level(logging.DEBUG)
    c = client(run, tmp_path, auth_method="client", token_file=str(token_file))
    assert ask(c).choices[0].message.content == "authed"
    answered = [f for f in run.responses() if "result" in f]
    assert answered == [{"jsonrpc": "2.0", "id": answered[0]["id"], "result": {"accessToken": TOKEN}}]
    assert any("acp out" in r.getMessage() for r in caplog.records)  # frames were logged at DEBUG
    for record in caplog.records:
        assert TOKEN not in record.getMessage()
        assert TOKEN[:12] not in record.getMessage()
    c.close()


def test_default_auth_registers_no_token_handler(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    run = fake(prompts=[[{"token": True}, {"text": "x"}]])
    c = client(run, tmp_path)
    ask(c)
    errors = [f for f in run.responses() if "error" in f]
    assert errors and errors[0]["error"]["code"] == -32601
    c.close()
