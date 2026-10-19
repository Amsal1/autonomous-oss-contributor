"""§14.18.4 delta 1: with several actions of one contribution open, each view resolves its own action
(``action_id`` in the token, never ``(cid, rev)``); after Confirm, navigation is read-only and comments
answer "Submitted…"."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.review import resolve
from tests.conftest import insert_action
from tests.telegram.reviewkit import comments, last_sent_id, open_card, publish, text, toast, token, view_message
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async def submit(approval_id: str) -> str:  # a LIVE-like submit: the action is done
        with db.transaction(ctx.conn):
            ctx.conn.execute("UPDATE outbound_actions SET status = 'done' WHERE id = (SELECT action_id FROM"
                             " approvals WHERE id = ?)", (approval_id,))  # fmt: skip
        return "done"

    async for r in make_rig(ctx, submit=submit):
        yield r


async def _comment_from_explorer(rig: TgRig, card_message: int, body: str) -> int:
    await rig.tap(token(rig, card_message, "🧭 Explore"), message=card_message)
    explorer = view_message(rig.ctx, "explorer")
    await rig.tap(token(rig, explorer, "💬 Comment"), message=explorer)
    await rig.say(body, reply_to=last_sent_id(rig))
    return explorer


async def test_open_pr_and_issue_claim_each_resolve_their_own_action(rig: TgRig) -> None:
    ctx = rig.ctx
    pr = await open_card(rig)
    claim = await publish(rig, insert_action(ctx, pr.cid, "issue_claim", {"target_number": 101, "body": "Mine."}))
    await _comment_from_explorer(rig, pr.message_id, "On the PR")
    (row,) = comments(ctx, pr.cid)
    assert row["approval_id"] == pr.approval
    await rig.say("Different words", reply_to=claim.message_id)  # rule 5 on a claim card: the replace panel
    assert len(comments(ctx, pr.cid)) == 1
    assert "Replace the comment text with your reply?" in rig.last_text()


async def test_push_update_and_comment_each_resolve_their_own_action(rig: TgRig) -> None:
    ctx = rig.ctx
    push = await open_card(rig, pr_number=7)
    await publish(rig, insert_action(ctx, push.cid, "comment", {"target_number": 7, "body": "Thanks!"}))
    await _comment_from_explorer(rig, push.message_id, "On the fix")
    (row,) = comments(ctx, push.cid)
    assert (row["approval_id"], row["rev"]) == (push.approval, 2)


async def test_after_confirm_navigation_is_read_only_and_comments_answer_submitted(rig: TgRig) -> None:
    pr = await open_card(rig)
    await rig.tap(token(rig, pr.message_id, "🧭 Explore"), message=pr.message_id)
    explorer = view_message(rig.ctx, "explorer")
    await rig.tap(token(rig, pr.message_id, "✅ Approve"), message=pr.message_id)
    await rig.tap(token(rig, pr.message_id, "🧪 Confirm dry run"), message=pr.message_id)
    assert "🚀 <b>Submitted</b>" in text(rig, pr.message_id)
    await rig.tap(token(rig, explorer, "▶"), message=explorer)
    assert "2/9" in text(rig, explorer)
    await rig.tap(token(rig, explorer, "💬 Comment"), message=explorer)
    assert toast(rig) == resolve.SUBMITTED
