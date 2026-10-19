"""§14.18.4 delta 5: draft comments are keyed by ``(cid, rev)``, so ``⬇️ Show card`` (a ``reissue()``)
keeps them and Send still works."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from ossbot.gatekeeper.context import AppContext
from tests.telegram.reviewkit import last_sent_id, newest_approval, open_card, status, text, toast, token, view_message
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


async def test_show_card_keeps_three_draft_comments(rig: TgRig) -> None:
    ctx = rig.ctx
    card = await open_card(rig)
    await rig.tap(token(rig, card.message_id, "🧭 Explore"), message=card.message_id)
    explorer = view_message(ctx, "explorer")
    for body in ("One", "Two", "Three"):
        await rig.tap(token(rig, explorer, "💬 Comment"), message=explorer)
        await rig.say(body, reply_to=last_sent_id(rig))
    await rig.tap(token(rig, explorer, "⬇️ Show card"), message=explorer)
    assert toast(rig) == "Re-sent the card"
    assert status(ctx, card.approval) == "invalidated"
    assert newest_approval(ctx, card.action) != card.approval
    draft = view_message(ctx, "draft_review")
    assert "3 comments" in text(rig, draft)
    await rig.tap(token(rig, draft, "📨 Send 3 comments"), message=draft)
    await rig.tap(token(rig, last_sent_id(rig), "📨 Send now"), message=last_sent_id(rig))
    (request,) = ctx.conn.execute("SELECT comments_json FROM change_requests WHERE cid = ?", (card.cid,)).fetchall()
    assert [c["text"] for c in json.loads(request["comments_json"])] == ["One", "Two", "Three"]
