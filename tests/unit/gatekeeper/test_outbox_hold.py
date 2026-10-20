"""Rows with buttons are never held or merged (DESIGN §14.13.3, AC 49): whatever the hold policy answers,
``Outbox.enqueue`` writes a row with tokens, an approval or an inline keyboard (and every P0/P1 row and
every non-holdable P2 row) as ``pending``, sent right away."""

from __future__ import annotations

from datetime import timedelta

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.outbox import HoldDecision, HoldRequest


def _status(ctx: AppContext, row_id: int | None) -> str:
    assert row_id is not None
    return str(db.one(ctx.conn, "SELECT status FROM tg_outbox WHERE id = ?", (row_id,))["status"])


@pytest.fixture
def always_hold(ctx: AppContext) -> list[HoldRequest]:
    seen: list[HoldRequest] = []

    def policy(req: HoldRequest) -> HoldDecision:
        seen.append(req)
        return HoldDecision(hold_until=ctx.clock.now() + timedelta(hours=8), silent=True)

    ctx.outbox.hold_policy = policy
    return seen


def test_a_holdable_fyi_is_held(ctx: AppContext, always_hold: list[HoldRequest]) -> None:
    rid = ctx.outbox.enqueue("sendMessage", {"chat_id": 1, "text": "fyi"}, holdable=True)
    assert _status(ctx, rid) == "held"
    assert ctx.outbox.due() == []


def test_rows_with_buttons_tokens_or_an_approval_are_never_held(ctx: AppContext,
                                                                always_hold: list[HoldRequest]) -> None:  # fmt: skip
    keyboard = {"inline_keyboard": [[{"text": "x", "callback_data": "t:abc"}]]}
    ids = [
        ctx.outbox.enqueue("sendMessage", {"chat_id": 1, "text": "k", "reply_markup": keyboard}, holdable=True),
        ctx.outbox.enqueue("sendMessage", {"chat_id": 1, "text": "t"}, bind_tokens=["tok"], holdable=True),
    ]
    assert [_status(ctx, i) for i in ids] == ["pending", "pending"]
    assert all(r.has_buttons for r in ctx.outbox.due())
    # sent right away, silently (§14.13.3)
    assert all(r.payload["disable_notification"] is True for r in ctx.outbox.due())
    assert [r.has_buttons for r in always_hold] == [True, True]


@pytest.mark.parametrize("priority", [0, 1])
def test_p0_and_p1_are_never_held(ctx: AppContext, always_hold: list[HoldRequest], priority: int) -> None:
    rid = ctx.outbox.enqueue("sendMessage", {"chat_id": 1, "text": "x"}, priority=priority, holdable=True)
    assert _status(ctx, rid) == "pending"


def test_a_non_holdable_p2_is_never_held(ctx: AppContext, always_hold: list[HoldRequest]) -> None:
    """Command replies, views and the security FYI are not FYIs to batch (§14.13.1)."""
    rid = ctx.outbox.enqueue("sendMessage", {"chat_id": 1, "text": "reply"})
    assert _status(ctx, rid) == "pending"
