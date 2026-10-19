"""S7 edit title/body/comment (DESIGN §14.11.8, §14.10.4 rule 5; AC 60, BA-13): the policy check runs
before anything is stored, every accepted edit invalidates (``text_edited``) and re-sends the card."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.review import edit_text
from tests.conftest import insert_action
from tests.telegram.reviewkit import (
    Card,
    card_of,
    labels,
    last_sent_id,
    latest,
    newest_approval,
    open_card,
    publish,
    sent_with,
    status,
    text,
    token,
)
from tests.telegram.tgharness import TgRig, make_rig

GHP = "ghp_" + "Q8xk2LmZ9vR4tYp7" + "Wn3bJc6Hd1Fs5Ga0Ke8U"


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def _contrib(ctx: AppContext, cid: str) -> tuple[str | None, str | None]:
    row = db.one(ctx.conn, "SELECT pr_title_override, pr_body_override FROM contributions WHERE id = ?", (cid,))
    return row[0], row[1]


async def _picker(rig: TgRig) -> tuple[Card, int]:
    card = await open_card(rig)
    await rig.tap(token(rig, card.message_id, "📝 Edit text"), message=card.message_id)
    picker = last_sent_id(rig)
    assert latest(rig, picker)["reply_parameters"]["message_id"] == card.message_id
    return card, picker


async def test_picker_offers_title_body_file_and_copy(rig: TgRig) -> None:
    _, picker = await _picker(rig)
    assert labels(rig, picker) == ["✏️ Title", "✏️ Body", "📄 Body as file", "📋 Copy title"]
    copy = rig.buttons(latest(rig, picker))[1][1]
    assert copy["copy_text"] == {"text": "Fix the thing"}


async def test_new_title_is_stored_and_the_card_resent(rig: TgRig) -> None:
    ctx = rig.ctx
    card, picker = await _picker(rig)
    await rig.tap(token(rig, picker, "✏️ Title"), message=picker)
    prompt = last_sent_id(rig)
    assert "<pre>Fix the thing</pre>" in text(rig, prompt)
    await rig.say("Fix the parser for empty input", reply_to=prompt)
    assert _contrib(ctx, card.cid)[0] == "Fix the parser for empty input"
    assert status(ctx, card.approval) == "invalidated"
    reason = db.one(ctx.conn, "SELECT invalidated_reason FROM approvals WHERE id = ?", (card.approval,))[0]
    assert reason == "text_edited"
    new = card_of(ctx, newest_approval(ctx, card.action))
    assert "PR title: <code>Fix the parser for empty input</code>   [edited by you]" in text(rig, new.message_id)
    assert sent_with(rig, "✏️ Saved: a new card follows (approve it again).")


@pytest.mark.parametrize("title", ["two\nlines", "x" * 257])
async def test_bad_titles_are_refused(rig: TgRig, title: str) -> None:
    card, picker = await _picker(rig)
    await rig.tap(token(rig, picker, "✏️ Title"), message=picker)
    await rig.say(title, reply_to=last_sent_id(rig))
    assert _contrib(rig.ctx, card.cid) == (None, None)
    assert "one line of 1-256 characters" in rig.last_text()
    assert status(rig.ctx, card.approval) == "pending"


async def test_a_body_with_a_secret_is_refused_before_storing(rig: TgRig) -> None:
    card, picker = await _picker(rig)
    await rig.tap(token(rig, picker, "✏️ Body"), message=picker)
    await rig.say(f"New body with {GHP}\n\nAI assistance disclosure", reply_to=last_sent_id(rig))
    assert _contrib(rig.ctx, card.cid) == (None, None)
    refusal = text(rig, sent_with(rig, "Not saved:"))
    assert GHP not in refusal
    assert status(rig.ctx, card.approval) == "pending"


async def test_clear_my_edits(rig: TgRig) -> None:
    ctx = rig.ctx
    card, picker = await _picker(rig)
    await rig.tap(token(rig, picker, "✏️ Title"), message=picker)
    await rig.say("Another title", reply_to=last_sent_id(rig))
    new = card_of(ctx, newest_approval(ctx, card.action))
    await rig.tap(token(rig, new.message_id, "📝 Edit text"), message=new.message_id)
    picker2 = last_sent_id(rig)
    assert labels(rig, picker2)[-1] == "↩️ Clear my edits"
    await rig.tap(token(rig, picker2, "↩️ Clear my edits"), message=picker2)
    assert _contrib(ctx, card.cid) == (None, None)
    assert status(ctx, new.approval) == "invalidated"


async def test_comment_card_edit_and_replace(rig: TgRig) -> None:
    ctx = rig.ctx
    cid_card = await open_card(rig, pr_number=7)
    action = insert_action(ctx, cid_card.cid, "comment", {"target_number": 7, "body": "Thanks!"})
    comment = await publish(rig, action)
    assert labels(rig, comment.message_id) == ["✅ Approve & Post", "❌ Reject", "📝 Edit text"]
    await rig.tap(token(rig, comment.message_id, "📝 Edit text"), message=comment.message_id)
    await rig.say("Thanks, rebased!", reply_to=last_sent_id(rig))
    body = json.loads(db.one(ctx.conn, "SELECT payload_json FROM outbound_actions WHERE id = ?",
                             (action,))[0])["body"]  # fmt: skip
    assert body == f"Thanks, rebased!\n\n{edit_text.MARKER}{action} -->"
    assert status(ctx, comment.approval) == "invalidated"
    new = card_of(ctx, newest_approval(ctx, action))
    await rig.say("Final words", reply_to=new.message_id)  # rule 5: the replace panel
    panel = last_sent_id(rig)
    await rig.tap(token(rig, panel, "✅ Replace"), message=panel)
    body = json.loads(db.one(ctx.conn, "SELECT payload_json FROM outbound_actions WHERE id = ?",
                             (action,))[0])["body"]  # fmt: skip
    assert body.startswith("Final words\n\n")


@pytest.mark.parametrize(
    ("name", "mime", "data", "error"),
    [
        ("body.md", "text/markdown", b"# Title\n", None),
        ("body.txt", None, b"plain", None),
        ("body.pdf", "application/pdf", b"%PDF", "send a .md or .txt document"),
        ("body.md", "text/markdown", b"\xff\xfe", "the file is not UTF-8"),
        ("body.md", "text/markdown", b"x" * (64 * 1024 + 1), "the limit is 64 KiB"),
    ],
)
def test_body_file_checks(name: str, mime: str | None, data: bytes, error: str | None) -> None:
    if error is None:
        assert edit_text.check_body_file(name, mime, data) == data.decode()
    else:
        with pytest.raises(ValueError, match=error):
            edit_text.check_body_file(name, mime, data)
