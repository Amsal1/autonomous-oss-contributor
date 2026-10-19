"""BA-6 (DESIGN §14.20.2): ``ViewKind`` equals the ``tg_views.kind`` CHECK, and a bad kind fails at
enqueue, before anything is sent."""

from __future__ import annotations

import re
from importlib import resources
from typing import get_args

import pytest

from ossbot.gatekeeper import outbox
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send


def _check_kinds() -> set[str]:
    sql = (resources.files("ossbot.gatekeeper.migrations") / "0002_decisions.sql").read_text(encoding="utf-8")
    block = re.search(r"kind TEXT NOT NULL CHECK \(kind IN \((.*?)\)\)", sql, re.S)
    assert block is not None
    return set(re.findall(r"'([a-z_]+)'", block.group(1)))


def test_view_kind_literal_equals_the_check() -> None:
    assert set(get_args(ui_send.ViewKind)) == _check_kinds() == set(outbox.VIEW_KINDS)


def test_bad_kind_fails_at_enqueue(ctx: AppContext) -> None:
    before = ctx.conn.execute("SELECT COUNT(*) FROM tg_outbox").fetchone()[0]
    with pytest.raises(ValueError):
        ui_send.deliver(ctx, k.plain("not_a_kind", k.line("x")))
    with pytest.raises(ValueError):
        ctx.outbox.enqueue("sendMessage", {"chat_id": 1, "text": "x"}, view={"kind": "bogus"})
    assert ctx.conn.execute("SELECT COUNT(*) FROM tg_outbox").fetchone()[0] == before
    assert ctx.conn.execute("SELECT COUNT(*) FROM ui_tokens").fetchone()[0] == 0


def test_mapped_view_kinds_are_valid(ctx: AppContext) -> None:
    for kind in ("picker", "confirm_prompt", "fyi", "chat_answer", "feature", "dashboard"):
        assert ui_send.deliver(ctx, k.plain(kind, k.line("x"))) is not None
