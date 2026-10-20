"""Coalescing and batching (DESIGN §14.13.3): an FYI with the same ``coalesce_key`` within 10 minutes edits
the earlier message; a P2 FYI inside a topic's 2-minute window is held to the window end; rows with
buttons are never coalesced, held or merged."""

from __future__ import annotations

from datetime import timedelta

import pytest

from ossbot.common.clock import from_iso
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.notify import BATCH_WINDOW, Notifier


@pytest.fixture
def notifier(ctx: AppContext) -> Notifier:
    n = Notifier(ctx)
    ctx.outbox.hold_policy = n.decide
    return n


def _send(ctx: AppContext, text: str, key: str = "c_1:ci", **kw: object) -> None:
    ctx.outbox.enqueue("sendMessage", {"chat_id": 1, "text": text}, coalesce_key=key, **kw)  # type: ignore[arg-type]


def _deliver_first(ctx: AppContext, message_id: int) -> None:
    row = ctx.outbox.due()[0]
    ctx.outbox.mark_sent(row, {"message_id": message_id})


def test_same_key_within_10_minutes_edits_the_earlier_message(ctx: AppContext, notifier: Notifier) -> None:
    _send(ctx, "CI: 3/9")
    _deliver_first(ctx, 77)
    ctx.clock.advance(minutes=5)  # type: ignore[attr-defined]
    _send(ctx, "CI: 7/9")
    out = notifier.prepare(ctx.outbox.due()[0])
    assert out.method == "editMessageText"
    assert out.payload["message_id"] == 77 and out.payload["text"] == "CI: 7/9"


def test_after_10_minutes_a_new_message_is_sent(ctx: AppContext, notifier: Notifier) -> None:
    _send(ctx, "CI: 3/9")
    _deliver_first(ctx, 77)
    ctx.clock.advance(minutes=11)  # type: ignore[attr-defined]
    _send(ctx, "CI: 9/9 ✅")
    assert notifier.prepare(ctx.outbox.due()[0]).method == "sendMessage"


def test_a_row_with_buttons_is_never_coalesced(ctx: AppContext, notifier: Notifier) -> None:
    _send(ctx, "CI: 3/9")
    _deliver_first(ctx, 77)
    _send(ctx, "CI: 7/9", bind_tokens=["tok"])
    assert notifier.prepare(ctx.outbox.due()[0]).method == "sendMessage"


def test_an_earlier_row_without_a_result_id_means_a_new_message(ctx: AppContext, notifier: Notifier) -> None:
    _send(ctx, "CI: 3/9")
    _deliver_first(ctx, 0)
    ctx.conn.execute("UPDATE tg_outbox SET result_message_id = NULL")
    _send(ctx, "CI: 7/9")
    assert notifier.prepare(ctx.outbox.due()[0]).method == "sendMessage"


def test_a_second_fyi_within_the_window_is_held_to_its_end(ctx: AppContext, notifier: Notifier) -> None:
    ctx.notify("📈 discovery found 2")
    first = ctx.clock.now()
    ctx.clock.advance(seconds=30)  # type: ignore[attr-defined]
    ctx.notify("📈 discovery found 3")
    held = db.one(ctx.conn, "SELECT next_attempt_at FROM tg_outbox WHERE status = 'held'")
    assert held is not None and from_iso(held[0]) == first + BATCH_WINDOW
    # a card part (buttons) in the same window still goes out at once, silently
    ctx.outbox.enqueue("sendMessage", {"chat_id": 1, "text": "card"}, bind_tokens=["tok"], thread_key="feed")
    assert db.one(ctx.conn, "SELECT COUNT(*) FROM tg_outbox WHERE status = 'held'")[0] == 1
    ctx.clock.advance(BATCH_WINDOW)  # type: ignore[attr-defined]
    assert notifier.release() == 1
    assert db.one(ctx.conn, "SELECT COUNT(*) FROM tg_outbox WHERE status = 'held'")[0] == 0


def test_window_closes_after_two_minutes(ctx: AppContext, notifier: Notifier) -> None:
    ctx.notify("📈 a")
    ctx.clock.advance(BATCH_WINDOW + timedelta(seconds=1))  # type: ignore[attr-defined]
    ctx.notify("📈 b")
    assert db.one(ctx.conn, "SELECT COUNT(*) FROM tg_outbox WHERE status = 'held'")[0] == 0
