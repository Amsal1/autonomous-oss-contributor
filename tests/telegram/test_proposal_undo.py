"""Declining a proposal and Undo (DESIGN §14.11.15; AC 61): Skip, Later and Never (after its confirm)
reject the ``p_`` approval with their reason, kill its Start-work token and move the contribution; Undo
(Skip and Later only, 10 min) sends a fresh card with the old expiry, whose Start work works."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ossbot.common.clock import FakeClock
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import proposal_actions
from tests.telegram.proposalkit import approval_status, card_of, proposal_card, state, tick
from tests.telegram.reviewkit import labels, last_sent_id, latest_send, text, toast, token
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def newest(ctx: AppContext, cid: str) -> str:
    row = db.one(ctx.conn, "SELECT id FROM proposal_approvals WHERE cid = ? ORDER BY created_at DESC, rowid DESC"
                 " LIMIT 1", (cid,))  # fmt: skip
    assert row is not None
    return str(row["id"])


async def test_skip_then_undo_sends_a_fresh_startable_card(rig: TgRig) -> None:
    card = await proposal_card(rig)
    start = token(rig, card.message_id, "✅ Start work")
    expires = db.one(rig.ctx.conn, "SELECT expires_at FROM proposal_approvals WHERE id = ?", (card.approval,))[0]  # type: ignore[index]
    await rig.tap(token(rig, card.message_id, "⏭ Skip"), message=card.message_id)
    assert toast(rig) == "Skipped. Undo within 10 min."
    assert approval_status(rig.ctx, card.approval) == ("rejected", "skipped")
    assert state(rig.ctx, card.cid) == "PROPOSAL_REJECTED"
    assert "⏭ <b>Skipped</b>" in text(rig, card.message_id)
    assert labels(rig, card.message_id) == ["↩️ Undo"]
    await rig.tap(start, message=card.message_id)  # the Start-work token died with the decision
    assert state(rig.ctx, card.cid) == "PROPOSAL_REJECTED"
    rig.ctx.clock.advance(minutes=9)  # type: ignore[attr-defined]
    await rig.tap(token(rig, card.message_id, "↩️ Undo"), message=card.message_id)
    assert toast(rig) == "↩️ Undone: a fresh card follows."
    assert state(rig.ctx, card.cid) == "PROPOSED"
    assert "↩️ <b>Undone</b> · see the card below" in text(rig, card.message_id)
    fresh = card_of(rig.ctx, newest(rig.ctx, card.cid))
    assert fresh.approval != card.approval
    assert db.one(rig.ctx.conn, "SELECT expires_at FROM proposal_approvals WHERE id = ?",
                  (fresh.approval,))[0] == expires  # type: ignore[index]  # fmt: skip
    assert latest_send(rig, fresh.message_id)["reply_parameters"]["message_id"] == card.message_id
    await rig.tap(token(rig, fresh.message_id, "✅ Start work"), message=fresh.message_id)
    await rig.tap(token(rig, fresh.message_id, "🛠 Confirm"), message=fresh.message_id)
    assert state(rig.ctx, card.cid) == "PROPOSAL_APPROVED"


async def test_undo_after_ten_minutes_is_too_late(rig: TgRig, clock: FakeClock) -> None:
    card = await proposal_card(rig)
    await rig.tap(token(rig, card.message_id, "⏭ Skip"), message=card.message_id)
    undo = token(rig, card.message_id, "↩️ Undo")
    clock.advance(minutes=11)
    await rig.tap(undo, message=card.message_id)
    assert toast(rig) in {proposal_actions.TOO_LATE, "This button has expired."}
    assert state(rig.ctx, card.cid) == "PROPOSAL_REJECTED"


async def test_later_picker_and_custom_days(rig: TgRig) -> None:
    card = await proposal_card(rig)
    await rig.tap(token(rig, card.message_id, "⏰ Later…"), message=card.message_id)
    assert labels(rig, card.message_id) == ["1 d", "3 d", "7 d", "14 d", "Custom…", "↩️ Back"]
    await rig.tap(token(rig, card.message_id, "↩️ Back"), message=card.message_id)
    assert labels(rig, card.message_id)[0] == "✅ Start work"
    await rig.tap(token(rig, card.message_id, "⏰ Later…"), message=card.message_id)
    await rig.tap(token(rig, card.message_id, "3 d"), message=card.message_id)
    assert approval_status(rig.ctx, card.approval) == ("rejected", "snoozed")
    assert state(rig.ctx, card.cid) == "PROPOSAL_SNOOZED"
    assert "⏰ <b>Snoozed until</b> <tg-time" in text(rig, card.message_id)
    assert labels(rig, card.message_id) == ["↩️ Undo"]


async def test_later_custom_is_a_force_reply(rig: TgRig) -> None:
    card = await proposal_card(rig)
    await rig.tap(token(rig, card.message_id, "⏰ Later…"), message=card.message_id)
    await rig.tap(token(rig, card.message_id, "Custom…"), message=card.message_id)
    prompt = last_sent_id(rig)
    assert latest_send(rig, prompt)["reply_markup"]["force_reply"] is True
    await rig.say("120", reply_to=prompt)
    assert state(rig.ctx, card.cid) == "PROPOSED"  # out of range: nothing changed
    await rig.tap(token(rig, card.message_id, "Custom…"), message=card.message_id)
    prompt = last_sent_id(rig)
    await rig.say("5", reply_to=prompt)
    assert state(rig.ctx, card.cid) == "PROPOSAL_SNOOZED"


async def test_never_repo_needs_its_confirm_and_has_no_undo(rig: TgRig) -> None:
    card = await proposal_card(rig)
    await rig.tap(token(rig, card.message_id, "🚫 Never…"), message=card.message_id)
    assert labels(rig, card.message_id) == ["🚫 This repo", "🚫 Kind: Bug", "↩️ Back"]
    await rig.tap(token(rig, card.message_id, "🚫 This repo"), message=card.message_id)
    assert "Never propose work from langfuse/langfuse again? Undo in /repos." in text(rig, card.message_id)
    assert state(rig.ctx, card.cid) == "PROPOSED"  # not yet
    await rig.tap(token(rig, card.message_id, "🚫 Never this repo"), message=card.message_id)
    assert approval_status(rig.ctx, card.approval) == ("rejected", "never_repo")
    assert rig.ctx.discovery_prefs.effective("langfuse/langfuse").never_repo is True
    assert "🚫 <b>Never: this repo</b>" in text(rig, card.message_id)
    assert labels(rig, card.message_id) == []


async def test_never_kind_names_the_kind_in_full(rig: TgRig) -> None:
    card = await proposal_card(rig, kind="refactor-small")
    await rig.tap(token(rig, card.message_id, "🚫 Never…"), message=card.message_id)
    await rig.tap(token(rig, card.message_id, "🚫 Kind: Small refactor"), message=card.message_id)
    assert ('Never propose "Small refactor" work (kind refactor-small) in any repo again? Undo in /filters → Kinds.'
            in text(rig, card.message_id))  # fmt: skip
    await rig.tap(token(rig, card.message_id, "🚫 Never this kind"), message=card.message_id)
    assert "refactor-small" in rig.ctx.discovery_prefs.effective().filters.excluded_kinds


async def test_skip_refused_while_the_confirm_is_armed(rig: TgRig) -> None:
    card = await proposal_card(rig)
    skip = token(rig, card.message_id, "⏭ Skip")
    await rig.tap(token(rig, card.message_id, "✅ Start work"), message=card.message_id)
    await rig.tap(skip, message=card.message_id)
    assert toast(rig) == "Cancel the Start-work confirm first"
    await tick(rig)
    assert state(rig.ctx, card.cid) == "PROPOSED"
