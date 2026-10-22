"""S4 comments and the draft review (DESIGN §14.11.5; AC 7, AC 8, AC 30, AC 32)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.review import comments as comments_mod
from tests.telegram.reviewkit import (
    Card,
    comments,
    labels,
    last_sent_id,
    latest,
    open_card,
    sent_with,
    status,
    text,
    toast,
    token,
    view_message,
)
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


async def _explorer(rig: TgRig) -> tuple[Card, int]:
    card = await open_card(rig)
    await rig.tap(token(rig, card.message_id, "🧭 Explore"), message=card.message_id)
    return card, view_message(rig.ctx, "explorer")


async def _comment(rig: TgRig, explorer: int, body: str) -> None:
    await rig.tap(token(rig, explorer, "💬 Comment"), message=explorer)
    await rig.say(body, reply_to=last_sent_id(rig))


async def test_saved_comment_acks_with_undo_and_a_draft_review(rig: TgRig) -> None:
    card, explorer = await _explorer(rig)
    await _comment(rig, explorer, "Add a changelog entry")
    (row,) = comments(rig.ctx, card.cid)
    assert row["path"] is None and row["approval_id"] == card.approval
    draft = view_message(rig.ctx, "draft_review")
    assert "Draft review" in text(rig, draft) and "1 comment" in text(rig, draft)
    assert "general — Add a changelog entry" in text(rig, draft)
    assert labels(rig, draft) == ["📨 Send 1 comment", "👁 Show all", "🗑 Discard"]
    await _comment(rig, explorer, "And a test")
    assert "2 comments" in text(rig, draft)  # the same draft message, edited
    assert view_message(rig.ctx, "draft_review") == draft


async def test_undo_removes_the_comment(rig: TgRig) -> None:
    card, explorer = await _explorer(rig)
    await _comment(rig, explorer, "Typo")
    ack = sent_with(rig, "Comment 1 saved")
    await rig.tap(token(rig, ack, "↩️ Undo"), message=ack)
    assert comments(rig.ctx, card.cid) == []
    assert toast(rig) == "↩️ Comment removed."


async def test_limits_refuse_before_storing(rig: TgRig) -> None:
    card, explorer = await _explorer(rig)
    await _comment(rig, explorer, "x" * 4001)
    assert comments(rig.ctx, card.cid) == []
    assert "the limit is 4,000" in rig.last_text()
    for i in range(comments_mod.MAX_COMMENTS):
        comments_mod.save(rig.ctx, card.cid, 1, card.action, f"c{i}", actor="test")
    assert comments_mod.save(rig.ctx, card.cid, 1, card.action, "one more", actor="test") == comments_mod.FULL
    assert len(comments(rig.ctx, card.cid)) == comments_mod.MAX_COMMENTS


async def test_send_creates_one_change_request_and_the_card_turns_s5(rig: TgRig) -> None:
    ctx = rig.ctx
    card, explorer = await _explorer(rig)
    await _comment(rig, explorer, "First")
    await _comment(rig, explorer, "Second")
    draft = view_message(ctx, "draft_review")
    await rig.tap(token(rig, draft, "📨 Send 2 comments"), message=draft)
    prompt = last_sent_id(rig)
    await rig.tap(token(rig, prompt, "📨 Send now"), message=prompt)
    assert "Change request sent (2 comments)" in toast(rig)
    (request,) = ctx.conn.execute("SELECT * FROM change_requests WHERE cid = ?", (card.cid,)).fetchall()
    assert request["text"] == "1. general — First\n\n2. general — Second"
    assert [c["text"] for c in json.loads(request["comments_json"])] == ["First", "Second"]
    assert db.one(ctx.conn, "SELECT state FROM contributions WHERE id = ?", (card.cid,))[0] == "CHANGES_REQUESTED"
    assert status(ctx, card.approval) == "invalidated"
    rig.frontend.publisher.recover()
    await rig.flush()
    s5 = text(rig, card.message_id)
    assert f"✏️ <b>Changes requested</b> · <code>{card.cid}</code> · r1 → r2 in progress\n<s>" in s5
    assert "Approve" not in str(latest(rig, card.message_id).get("reply_markup"))


async def test_send_with_free_text_via_the_prompt(rig: TgRig) -> None:
    card, explorer = await _explorer(rig)
    await _comment(rig, explorer, "Only one")
    draft = view_message(rig.ctx, "draft_review")
    await rig.tap(token(rig, draft, "📨 Send 1 comment"), message=draft)
    await rig.say("Please also rebase", reply_to=last_sent_id(rig))
    (request,) = rig.ctx.conn.execute("SELECT text FROM change_requests WHERE cid = ?", (card.cid,)).fetchall()
    assert request["text"] == "Please also rebase\n\n1. general — Only one"


async def test_discard_clears_the_draft(rig: TgRig) -> None:
    card, explorer = await _explorer(rig)
    await _comment(rig, explorer, "Gone soon")
    draft = view_message(rig.ctx, "draft_review")
    await rig.tap(token(rig, draft, "🗑 Discard"), message=draft)
    assert comments(rig.ctx, card.cid) == []
    assert "0 comments" in text(rig, draft) and labels(rig, draft) == []


async def test_comment_while_armed_refreshes_the_confirm_panel(rig: TgRig) -> None:
    ctx = rig.ctx
    card, explorer = await _explorer(rig)
    await rig.tap(token(rig, card.message_id, "✅ Approve"), message=card.message_id)
    confirm = token(rig, card.message_id, "🧪 Confirm dry run")
    await _comment(rig, explorer, "Late thought")
    assert "Confirm pending" in rig.last_text()
    assert "⚠️ 1 unsent comment will be discarded" in text(rig, card.message_id)
    assert token(rig, card.message_id, "🧪 Confirm dry run") == confirm  # same tokens, no rotate
    draft = view_message(ctx, "draft_review")
    await rig.tap(token(rig, draft, "📨 Send 1 comment"), message=draft)
    await rig.tap(token(rig, last_sent_id(rig), "📨 Send now"), message=last_sent_id(rig))
    assert "Cancel the Confirm first" in toast(rig)
    assert status(ctx, card.approval) == "armed"
