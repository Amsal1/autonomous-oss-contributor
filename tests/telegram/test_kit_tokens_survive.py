"""§14.18.4 delta 1: review kit tokens carry ``{cid, rev, action_id}`` and no approval id, so Cancel and
``reissue()`` never kill navigation, comment undo or the draft review; an invalidation by a newer
revision answers "Superseded" through ``tg_views.stale``."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.review import resolve
from tests.telegram.reviewkit import Card, last_sent_id, open_card, sent_with, text, toast, token, view_message
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


async def _views(rig: TgRig) -> tuple[Card, int, int, int]:
    """Card, explorer, file view and the ack of one saved comment."""
    card = await open_card(rig)
    await rig.tap(token(rig, card.message_id, "🧭 Explore"), message=card.message_id)
    explorer = view_message(rig.ctx, "explorer")
    await rig.tap(token(rig, card.message_id, "🧾 Files"), message=card.message_id)
    file_view = view_message(rig.ctx, "file")
    await rig.tap(token(rig, explorer, "💬 Comment"), message=explorer)
    await rig.say("Keep this", reply_to=last_sent_id(rig))
    return card, explorer, file_view, sent_with(rig, "Comment 1 saved")


async def _still_work(rig: TgRig, card: Card, explorer: int, file_view: int) -> None:
    await rig.tap(token(rig, explorer, "▶"), message=explorer)
    assert "2/9" in text(rig, explorer)
    await rig.tap(token(rig, file_view, "Next file ▶"), message=file_view)
    assert "file 2/2" in text(rig, file_view)
    draft = view_message(rig.ctx, "draft_review")
    await rig.tap(token(rig, draft, "📨 Send 1 comment"), message=draft)
    assert "Anything to add?" in text(rig, last_sent_id(rig))


async def test_cancel_keeps_every_review_token(rig: TgRig) -> None:
    card, explorer, file_view, _ = await _views(rig)
    await rig.tap(token(rig, card.message_id, "✅ Approve"), message=card.message_id)
    await rig.tap(token(rig, card.message_id, "↩️ Cancel"), message=card.message_id)
    await _still_work(rig, card, explorer, file_view)


async def test_comment_undo_survives_cancel(rig: TgRig) -> None:
    card, _, _, ack = await _views(rig)
    await rig.tap(token(rig, card.message_id, "✅ Approve"), message=card.message_id)
    await rig.tap(token(rig, card.message_id, "↩️ Cancel"), message=card.message_id)
    await rig.tap(token(rig, ack, "↩️ Undo"), message=ack)
    assert toast(rig) == "↩️ Comment removed."


async def test_reissue_keeps_every_review_token(rig: TgRig) -> None:
    card, explorer, file_view, _ = await _views(rig)
    (new,) = rig.ctx.reissue(card.cid, "test", keep_expiry=True)
    await rig.flush()
    assert new != card.approval
    await _still_work(rig, card, explorer, file_view)


async def test_newer_revision_supersedes_the_views(rig: TgRig) -> None:
    ctx = rig.ctx
    card, explorer, file_view, _ = await _views(rig)
    with db.transaction(ctx.conn):
        ctx.approvals.invalidate_contribution(card.cid, "new_package_rev", "system")
    rig.frontend.publisher.recover()
    await rig.flush()
    for mid in (explorer, file_view):
        assert db.one(ctx.conn, "SELECT stale FROM tg_views WHERE message_id = ?", (mid,))[0] == 1
    await rig.tap(token(rig, explorer, "▶"), message=explorer)
    assert toast(rig) == resolve.SUPERSEDED
    assert "1/9" in text(rig, explorer)  # nothing was edited
