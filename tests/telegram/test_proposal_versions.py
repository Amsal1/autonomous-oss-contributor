"""Proposal versions (DESIGN §14.11.16; AC 38, AC 10): Adjust plan and Make it smaller send scout feedback
and disable the card, the next version replies to the previous card with the deterministic "Changed
since" block, the old card goes S12 with Show current, and its tokens answer "Superseded…"."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import proposal_cards
from tests.telegram.proposalkit import proposal_card, revise
from tests.telegram.reviewkit import labels, last_sent_id, latest, latest_send, text, toast, token
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def feedback(ctx: AppContext, cid: str) -> str | None:
    row = db.one(ctx.conn, "SELECT proposal_feedback_json FROM contributions WHERE id = ?", (cid,))
    return None if row is None else row[0]


def test_version_diff_is_deterministic() -> None:
    old = {"problem": "a", "approach": ["one", "two", "Remove the debug log"], "areas": ["x.ts"],
           "estimate": {"code_lines": 35, "test_lines": 40}, "_class": "s", "validation_plan": "v",
           "risks": []}  # fmt: skip
    new = {**old, "approach": ["one", "two", "Gate the debug log behind EXPORT_DEBUG"], "areas": ["x.ts", "c.ts"],
           "estimate": {"code_lines": 48, "test_lines": 40}}  # fmt: skip
    diff = proposal_cards.version_diff(old, new)
    assert diff == ["+ Approach 3: Gate the debug log behind EXPORT_DEBUG", "− Approach 3: Remove the debug log",
                    "+ File: c.ts", "Size: s → s · ~35 → ~48 code · ~40 → ~40 test"]  # fmt: skip
    assert proposal_cards.version_diff(old, old) == []
    assert proposal_cards.version_diff(old, new) == diff


async def test_adjust_plan_prompt_then_v2_replies_with_the_diff(rig: TgRig) -> None:
    v1 = await proposal_card(rig, approach=["one", "two", "Remove the debug log"])
    await rig.tap(token(rig, v1.message_id, "✏️ Adjust plan"), message=v1.message_id)
    prompt = last_sent_id(rig)
    sent = latest_send(rig, prompt)
    assert sent["reply_markup"]["force_reply"] is True
    assert sent["reply_markup"]["input_field_placeholder"] == "What should change in the plan? (≤ 2000)"
    await rig.say("keep the debug log, behind a flag", reply_to=prompt)
    assert '"kind": "adjust"' in str(feedback(rig.ctx, v1.cid))
    assert labels(rig, v1.message_id) == ["⛔ Revising v1 → v2…"]
    v2 = await revise(rig, v1.cid, 1, approach=["one", "two", "Gate the debug log behind EXPORT_DEBUG"])
    assert v2.version == 2
    body = text(rig, v2.message_id)
    assert "<b>Changed since v1</b> (diff by gatekeeper; content 🤖):" in body
    assert "+ Approach 3: Gate the debug log behind EXPORT_DEBUG" in body
    assert "− Approach 3: Remove the debug log" in body
    assert latest_send(rig, v2.message_id)["reply_parameters"]["message_id"] == v1.message_id
    # the old card: S12 superseded, Not decidable + Show current
    old = text(rig, v1.message_id)
    assert "⚪ <b>Superseded</b> · v2 is newer" in old
    assert labels(rig, v1.message_id)[0].startswith("⛔")
    assert "⬇️ Show current" in labels(rig, v1.message_id)


async def test_reply_to_the_card_is_adjust_plan(rig: TgRig) -> None:
    v1 = await proposal_card(rig)
    await rig.say("smaller please, no refactor", reply_to=v1.message_id)
    assert "smaller please, no refactor" in str(feedback(rig.ctx, v1.cid))


async def test_make_it_smaller_sends_the_fixed_feedback(rig: TgRig) -> None:
    v1 = await proposal_card(rig)
    await rig.tap(token(rig, v1.message_id, "📏 Make it smaller"), message=v1.message_id)
    assert "Reduce the scope to fit class xs; keep the fix minimal" in str(feedback(rig.ctx, v1.cid))
    assert toast(rig) == "✏️ Sent to scout; v2 follows."


async def test_xs_card_has_no_make_it_smaller(rig: TgRig) -> None:
    v1 = await proposal_card(rig, estimate={"files": 1, "code_lines": 3, "test_lines": 5, "class": "xs"})
    assert "📏 Make it smaller" not in labels(rig, v1.message_id)


async def test_v1_tokens_after_v2_answer_superseded(rig: TgRig) -> None:
    v1 = await proposal_card(rig)
    skip = token(rig, v1.message_id, "⏭ Skip")
    start = token(rig, v1.message_id, "✅ Start work")
    await rig.tap(token(rig, v1.message_id, "📏 Make it smaller"), message=v1.message_id)
    await revise(rig, v1.cid, 1, estimate={"files": 1, "code_lines": 10, "test_lines": 5, "class": "xs"})
    await rig.tap(skip, message=v1.message_id)
    assert toast(rig) == proposal_cards.SUPERSEDED
    await rig.tap(start, message=v1.message_id)
    assert toast(rig).startswith("Superseded")


async def test_show_current_on_v1_resends_v2(rig: TgRig) -> None:
    v1 = await proposal_card(rig)
    await rig.tap(token(rig, v1.message_id, "📏 Make it smaller"), message=v1.message_id)
    v2 = await revise(rig, v1.cid, 1, estimate={"files": 1, "code_lines": 10, "test_lines": 5, "class": "xs"})
    before = db.one(rig.ctx.conn, "SELECT expires_at FROM proposal_approvals WHERE id = ?", (v2.approval,))[0]  # type: ignore[index]
    await rig.tap(token(rig, v1.message_id, "⬇️ Show current"), message=v1.message_id)
    newest = db.one(rig.ctx.conn, "SELECT id, version, expires_at FROM proposal_approvals WHERE cid = ? ORDER BY"
                    " created_at DESC, rowid DESC LIMIT 1", (v1.cid,))  # fmt: skip
    assert newest is not None and newest["id"] != v2.approval and newest["version"] == 2
    assert newest["expires_at"] == before
    assert "Proposal" in str(latest(rig, last_sent_id(rig)).get("text", ""))
