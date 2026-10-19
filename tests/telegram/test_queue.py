"""The approval queue (DESIGN §14.11.13; AC 11, AC 43): deterministic class ordering, gate 2 before
gate 1, proposals promoted within 6 h of expiry, and ``📥 Open n`` re-sending with the expiry kept."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from ossbot.common.clock import to_iso
from ossbot.common.ids import new_id
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.review import queue
from tests.conftest import insert_action, insert_contribution, packaged_contribution
from tests.telegram.reviewkit import labels, newest_approval, publish, sent_with, text, toast, token
from tests.telegram.tgharness import TgRig, make_rig


def _approve(ctx: AppContext, action: str, *, hours: int = 72) -> str:
    with db.transaction(ctx.conn):
        approval = ctx.create_approval(action, "test")
        ctx.conn.execute("UPDATE approvals SET expires_at = ? WHERE id = ?",
                         (to_iso(ctx.clock.now() + timedelta(hours=hours)), approval))  # fmt: skip
    return approval


def _proposal(ctx: AppContext, *, hours: int, source: str = "discover", score: float = 0.5) -> str:
    cid = insert_contribution(ctx, State.PROPOSED, source=source, est_class="s", score=score)
    now = ctx.clock.now()
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO proposal_approvals (id, cid, version, binding_json, binding_hash, status, tg_chat_id,"
            " created_at, expires_at) VALUES (?,?,1,'{}','h','pending',?,?,?)",
            (new_id("p"), cid, ctx.telegram_chat_id, to_iso(now), to_iso(now + timedelta(hours=hours))),
        )
    return cid


def test_class_order_and_gates(ctx: AppContext) -> None:
    pr_cid, pr = packaged_contribution(ctx)
    _approve(ctx, pr, hours=10)  # class 3 (open_pr) even though it expires first
    push_cid, push = packaged_contribution(ctx, pr_number=7)
    _approve(ctx, push, hours=50)  # class 2
    ours = insert_action(ctx, push_cid, "comment", {"target_number": 7, "body": "On our PR"})
    _approve(ctx, ours, hours=70)  # class 0: someone is waiting
    other = insert_action(ctx, pr_cid, "comment", {"target_number": 99, "body": "Elsewhere"})
    _approve(ctx, other, hours=1)  # class 5
    user_cid, user_action = packaged_contribution(ctx)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE contributions SET source = 'user' WHERE id = ?", (user_cid,))
    _approve(ctx, user_action, hours=60)  # class 1 (👤)
    late = _proposal(ctx, hours=3)  # promoted to class 2 within 6 h
    early = _proposal(ctx, hours=40, score=0.9)  # class 4
    order = [(i.gate, i.class_rank, i.subject) for i in queue.ordered(ctx)]
    assert order == [(2, 0, push_cid), (2, 1, user_cid), (2, 2, push_cid), (2, 3, pr_cid), (2, 5, pr_cid),
                     (1, 2, late), (1, 4, early)]  # fmt: skip
    oldest = [(i.gate, i.subject) for i in queue.ordered(ctx, "oldest")]
    assert oldest[0] == (2, pr_cid) and oldest[-1] == (1, early)


def test_maintainer_fix_expired_items_and_size_cards(ctx: AppContext) -> None:
    """F009-R1-8: a maintainer-triggered push_update is class 0; expired approvals aren't listed even
    before the sweep; ceiling-hit (class 2) and RESCOPE_REQUIRED (class 3) items are listed."""
    fixed_cid, fixed = packaged_contribution(ctx, pr_number=8)
    with db.transaction(ctx.conn):
        ctx.audit.append("uid:2001/worker", "transition", {"from": "WATCHING", "to": "IMPLEMENTING",
                                                           "evidence": {"reason": "fix_round"}}, fixed_cid)  # fmt: skip
    _approve(ctx, fixed, hours=60)
    _, gone = packaged_contribution(ctx)
    _approve(ctx, gone, hours=-1)  # past expires_at, still 'pending' until the sweep
    ceiling = insert_contribution(ctx, State.RESCOPE_REQUIRED, size_report_json='{"rescope_reason": "ceiling"}')
    overrun = insert_contribution(ctx, State.RESCOPE_REQUIRED, size_report_json='{"rescope_reason": "overrun"}')
    order = [(i.class_rank, i.subject, i.family) for i in queue.ordered(ctx)]
    assert order == [(0, fixed_cid, "a"), (2, ceiling, "r"), (3, overrun, "r")]


def test_autonomous_proposals_rank_by_score_not_target_priority() -> None:
    when = datetime(2026, 10, 19, tzinfo=UTC)
    a = queue.Item(1, 4, when, 9, "t", "p", "c_A", "a", score=0.2)
    b = queue.Item(1, 4, when, 0, "t", "p", "c_B", "b", score=0.8)
    assert sorted([a, b], key=lambda i: i.key("priority")) == [b, a]
    c = queue.Item(2, 3, when, 0, "t", "a", "c_C", "c")
    d = queue.Item(2, 3, when, 5, "t", "a", "c_D", "d")
    assert sorted([c, d], key=lambda i: i.key("priority")) == [d, c]  # elsewhere the target priority wins


def test_empty_queue_and_paging(ctx: AppContext) -> None:
    assert "Nothing is waiting for you." in str(queue.view(ctx).blocks)
    for _ in range(7):
        _, action = packaged_contribution(ctx)
        _approve(ctx, action)
    view = queue.view(ctx, page=2)
    assert view.state == {"page": 2, "sort": "priority"}
    assert [b.label for b in view.rows[0]] == ["📥 Open 6", "📥 Open 7"]


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


async def test_queue_command_and_open_keep_the_expiry(rig: TgRig) -> None:
    ctx = rig.ctx
    cid, action = packaged_contribution(ctx)
    card = await publish(rig, action)
    before = db.one(ctx.conn, "SELECT expires_at FROM approvals WHERE id = ?", (card.approval,))[0]
    ctx.clock.advance(hours=2)  # type: ignore[attr-defined]
    await rig.say("/queue")
    mid = sent_with(rig, "Queue")
    assert "📥 <b>Queue</b> · 1 open" in text(rig, mid)
    assert labels(rig, mid) == ["📥 Open 1", "⬆️ Oldest first", "✅ 🎯 By priority"]
    await rig.tap(token(rig, mid, "📥 Open 1"), message=mid)
    assert toast(rig) == "Re-sent the card"
    new = newest_approval(ctx, action)
    assert new != card.approval
    assert db.one(ctx.conn, "SELECT expires_at FROM approvals WHERE id = ?", (new,))[0] == before
    await rig.tap(token(rig, mid, "⬆️ Oldest"), message=mid)
    assert labels(rig, mid)[-2:] == ["✅ ⬆️ Oldest first", "🎯 By priority"]
    assert cid in str(ctx.conn.execute("SELECT cid FROM approvals a JOIN outbound_actions o ON o.id = a.action_id"
                                       " WHERE a.id = ?", (new,)).fetchone()[0])  # fmt: skip
