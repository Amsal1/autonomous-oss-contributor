"""Post-v1 OD-C1 (DESIGN §14.11.12): a stale card always has a way forward. After a reload, a start-time
snapshot change, kill -> resume and pause (+ dry-run toggle) -> resume the bot sends a fresh card and
the stale one offers ``⬇️ Show current``; while paused or killed Show current answers that the card
follows after /resume and creates nothing; repeated restarts send no duplicate card."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext, build_context
from ossbot.gatekeeper.telegram.review import stale
from tests.conftest import TEST_ROLES
from tests.telegram.reviewkit import Card, card_of, labels, newest_approval, open_card, status, text, toast, token
from tests.telegram.tgharness import TgRig, make_rig

SHOW = "⬇️ Show current"


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


async def _tick(rig: TgRig) -> None:
    rig.frontend.publisher.recover()
    await rig.flush()


def _touch_config(ctx: AppContext) -> None:
    path = ctx.paths.config_dir / "telegram.yaml"
    path.write_bytes(path.read_bytes() + b"\n# changed by a test\n")


def _resume(ctx: AppContext) -> None:
    ctx.control.request_resume("tg:4242")
    for flag in (ctx.paths.killed_flag, ctx.paths.paused_flag, ctx.paths.resume_request):
        flag.unlink(missing_ok=True)  # what the root enforcer does
    assert ctx.control.reconcile().state == "running"


def _open_count(ctx: AppContext, action: str) -> int:
    return int(ctx.conn.execute("SELECT COUNT(*) FROM approvals WHERE action_id = ? AND status IN"
                                " ('pending','armed')", (action,)).fetchone()[0])  # fmt: skip


def _fresh_card(rig: TgRig, old: Card) -> Card:
    fresh = card_of(rig.ctx, newest_approval(rig.ctx, old.action))
    assert fresh.approval != old.approval and fresh.message_id != old.message_id
    assert any("Approve" in x for x in labels(rig, fresh.message_id))  # armed: the two-tap starts on it
    return fresh


def _stale_shows_current(rig: TgRig, old: Card) -> None:
    assert labels(rig, old.message_id) == ["⛔ Not approvable", SHOW]
    assert f"<code>{old.cid}</code>" in text(rig, old.message_id).split("\n")[0]


async def test_reload(rig: TgRig) -> None:
    ctx = rig.ctx
    card = await open_card(rig)
    _touch_config(ctx)
    preview = ctx.snapshots.prepare_reload()
    _, invalidated = ctx.snapshots.apply_reload(preview.hash, "tg:4242")
    ctx.reissue_cards(invalidated, "tg:4242")
    await _tick(rig)
    _fresh_card(rig, card)
    _stale_shows_current(rig, card)


async def test_start_time_snapshot_change_and_restarts(rig: TgRig) -> None:
    ctx = rig.ctx
    card = await open_card(rig)
    _touch_config(ctx)
    for _ in range(3):  # a start on the changed config, then two restarts on the same one
        again = build_context(ctx.paths, TEST_ROLES, clock=ctx.clock, artifacts=ctx.approvals.artifacts, conn=ctx.conn)
        again.snapshots.load_at_start()
        ctx.snapshots = again.snapshots  # the rig's context follows the restarted snapshot
        await _tick(rig)
    _fresh_card(rig, card)
    _stale_shows_current(rig, card)
    assert _open_count(ctx, card.action) == 1
    sent = [r[0] for r in ctx.conn.execute("SELECT dedupe_key FROM tg_outbox WHERE dedupe_key LIKE 'card:%'")]
    assert len(sent) == len(set(sent)) == 2


async def test_kill_then_resume_and_show_current_while_killed(rig: TgRig) -> None:
    ctx = rig.ctx
    card = await open_card(rig)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE approvals SET status = 'armed' WHERE id = ?", (card.approval,))
    ctx.control.kill("test", "tg:4242")
    await _tick(rig)
    assert status(ctx, card.approval) == "invalidated"
    _stale_shows_current(rig, card)  # owed: Show current even before the re-send
    before = int(ctx.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0])
    await rig.tap(token(rig, card.message_id, SHOW), message=card.message_id)
    assert toast(rig) == stale.DEFERRED
    assert int(ctx.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]) == before
    _resume(ctx)
    await _tick(rig)
    fresh = _fresh_card(rig, card)
    assert _open_count(ctx, card.action) == 1 and status(ctx, fresh.approval) == "pending"


@pytest.mark.parametrize("toggle", [False, True])
async def test_pause_then_resume(rig: TgRig, toggle: bool) -> None:
    ctx = rig.ctx
    card = await open_card(rig)
    ctx.control.pause("test", "tg:4242")
    if toggle:
        ctx.reissue_cards(ctx.control.set_dry_run(False, "tg:4242"), "tg:4242")
    else:
        with db.transaction(ctx.conn):
            ctx.approvals.invalidate_contribution(card.cid, "killed", "system")  # e.g. a kill lifted to a pause
    await _tick(rig)
    assert _open_count(ctx, card.action) == 0
    _stale_shows_current(rig, card)
    _resume(ctx)
    await _tick(rig)
    _fresh_card(rig, card)
    assert _open_count(ctx, card.action) == 1


async def test_show_current_after_resume_resends_exactly_one(rig: TgRig) -> None:
    """The owed card was not re-sent yet (the tap comes before the next tick): the tap sends it."""
    ctx = rig.ctx
    card = await open_card(rig)
    ctx.control.pause("test", "tg:4242")
    with db.transaction(ctx.conn):
        ctx.approvals.invalidate_contribution(card.cid, "config_snapshot_changed", "system")
    await _tick(rig)
    _resume(ctx)
    await rig.tap(token(rig, card.message_id, SHOW), message=card.message_id)
    assert toast(rig) == "Re-sent the card"
    assert _open_count(ctx, card.action) == 1
    await _tick(rig)
    assert _open_count(ctx, card.action) == 1  # the sweep finds nothing owed any more
