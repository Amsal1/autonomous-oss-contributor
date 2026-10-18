"""Delta 7 (DESIGN §14.18.4.3, AC 46): re-sends keep the expiry; the sweep reference is the newest
approval's window start, so navigation can't postpone the contribution-expiry sweep."""

from __future__ import annotations

from datetime import datetime, timedelta

from ossbot.common.clock import from_iso
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from tests.conftest import insert_action, packaged_contribution

TTL = timedelta(hours=72)


def _expiry(ctx: AppContext, approval: str) -> datetime:
    row = db.one(ctx.conn, "SELECT expires_at FROM approvals WHERE id = ?", (approval,))
    assert row is not None
    return from_iso(row["expires_at"])


def test_show_card_keeps_the_window(ctx: AppContext) -> None:
    cid, action = packaged_contribution(ctx)
    t0 = ctx.clock.now()
    first = ctx.create_approval(action, "system")
    ctx.clock.advance(hours=70)  # type: ignore[attr-defined]
    [second] = ctx.reissue(cid, "tg:4242", keep_expiry=True)
    assert _expiry(ctx, second) == t0 + TTL == _expiry(ctx, first)
    ctx.clock.advance(timedelta(hours=1, minutes=57))  # type: ignore[attr-defined]  # under 5 min left
    [third] = ctx.reissue(cid, "tg:4242", keep_expiry=True)
    assert _expiry(ctx, third) == ctx.clock.now() + TTL


def test_retry_after_expiry_gets_a_fresh_window(ctx: AppContext) -> None:
    cid, action = packaged_contribution(ctx)
    ctx.create_approval(action, "system")
    ctx.clock.advance(hours=73)  # type: ignore[attr-defined]
    ctx.approvals.expire_due()
    [new] = ctx.reissue(cid, "tg:4242", keep_expiry=True)
    assert _expiry(ctx, new) == ctx.clock.now() + TTL


def test_keep_expiry_false_is_todays_behaviour(ctx: AppContext) -> None:
    cid, action = packaged_contribution(ctx)
    ctx.create_approval(action, "system")
    ctx.clock.advance(hours=10)  # type: ignore[attr-defined]
    [new] = ctx.reissue(cid, "tg:4242", keep_expiry=False)
    assert _expiry(ctx, new) == ctx.clock.now() + TTL


def test_dry_run_toggle_reissue_gets_a_fresh_window(ctx: AppContext) -> None:
    _, action = packaged_contribution(ctx)
    ctx.create_approval(action, "system")
    ctx.clock.advance(hours=10)  # type: ignore[attr-defined]
    [new] = ctx.reissue_cards(ctx.control.set_dry_run(False, "op"), "op")
    assert _expiry(ctx, new) == ctx.clock.now() + TTL


def test_open_sibling_keeps_its_own_expiry(ctx: AppContext) -> None:
    cid, action = packaged_contribution(ctx)
    t0 = ctx.clock.now()
    ctx.create_approval(action, "system")
    ctx.clock.advance(hours=5)  # type: ignore[attr-defined]
    claim = insert_action(ctx, cid, "issue_claim", {"target_number": 101, "body": "I'd like to work on this."})
    ctx.create_approval(claim, "system")
    t1 = ctx.clock.now()
    ctx.clock.advance(hours=1)  # type: ignore[attr-defined]
    new = ctx.reissue(cid, "tg:4242", keep_expiry=True)
    assert sorted(_expiry(ctx, a) for a in new) == [t0 + TTL, t1 + TTL]


def test_sweep_reference_is_the_window_start(ctx: AppContext) -> None:
    """A re-send at T+70 h inherits the window T..T+72 h: at T + 7 d + 1 h the sweep moves the
    contribution (by created_at, T+70 h, it would not have moved yet)."""
    cid, action = packaged_contribution(ctx)
    ctx.create_approval(action, "system")
    ctx.clock.advance(hours=70)  # type: ignore[attr-defined]
    ctx.reissue(cid, "tg:4242", keep_expiry=True)
    days = ctx.config.policy.limits.contribution_expiry_days
    ctx.clock.advance(timedelta(days=days, hours=1) - timedelta(hours=70))  # type: ignore[attr-defined]
    ctx.approvals.expire_due()
    assert ctx.state_machine.sweep_expired() == [cid]
    row = db.one(ctx.conn, "SELECT state FROM contributions WHERE id = ?", (cid,))
    assert row is not None and row["state"] == State.EXPIRED.value
    statuses = {r[0] for r in ctx.conn.execute("SELECT status FROM outbound_actions WHERE cid = ?", (cid,))}
    assert statuses == {"cancelled"}  # the EXPIRED move cancels pending_approval actions


def test_inherited_window_does_not_postpone_the_sweep(ctx: AppContext) -> None:
    cid, action = packaged_contribution(ctx)
    t0 = ctx.clock.now()
    first = ctx.create_approval(action, "system")
    ctx.clock.advance(hours=70)  # type: ignore[attr-defined]
    [kept] = ctx.reissue(cid, "tg:4242", keep_expiry=True)
    assert _expiry(ctx, kept) == _expiry(ctx, first)
    window_start = _expiry(ctx, kept) - TTL
    assert window_start == t0  # not the re-send's created_at (T + 70 h)
