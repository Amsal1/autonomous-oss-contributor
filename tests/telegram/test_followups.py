"""Follow-up suggestions (DESIGN §14.12.4, AC 19, AC 37, U2, U14)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from ossbot.gatekeeper.chat import followups
from ossbot.gatekeeper.context import AppContext
from tests.telegram.chatkit import FakeBackend, deltas, wire
from tests.telegram.tgharness import TgRig, make_rig

GHP = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
ANSWER = "Terraform is declarative.\n\n```followups\n- Compare with OpenTofu\n- What about state files?\n```"


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def test_extract_strips_the_block_and_keeps_at_most_three() -> None:
    body, labels = followups.extract("Answer\n```followups\n- a\n- b\n- c\n- d\n```")
    assert body == "Answer" and labels == ["a", "b", "c"]


@pytest.mark.parametrize("item", [
    "x" * 65, "/kill now", "Approve the PR", "please confirm", "submit it", "resume work", "dry run off",
    "zero\u200bwidth", "bidi\u202eflip", f"token {GHP}",
])  # fmt: skip
def test_unsafe_items_are_dropped_never_cut(item: str) -> None:
    assert followups.extract(f"A\n```followups\n- {item}\n- fine one\n```")[1] == ["fine one"]


def test_a_64_character_item_is_kept_whole() -> None:
    item = "y" * 64
    assert followups.extract(f"A\n```followups\n- {item}\n```")[1] == [item]


async def test_a_tap_echoes_first_then_sends_exactly_the_label(rig: TgRig) -> None:
    backend = wire(rig.ctx, FakeBackend(deltas(ANSWER)))
    await rig.say("what is terraform?")
    final = rig.sent("sendRichMessage")[-1]
    assert "followups" not in json.dumps(final)
    token = rig.token_of(final, "Compare with OpenTofu")
    answer_mid = rig.message_id_of(-1, "sendRichMessage")
    rig.req.calls.clear()
    await rig.tap(token, message=answer_mid)
    assert backend.calls[-1][0] == [{"type": "input_text", "text": "Compare with OpenTofu"}]
    methods = [(m, json.dumps(p, ensure_ascii=False)) for m, p in rig.req.calls if m.startswith("send")]
    echo = next(i for i, (m, p) in enumerate(methods) if "↪️ Compare with OpenTofu" in p)
    answer = next(i for i, (m, p) in enumerate(methods) if m == "sendRichMessage")
    assert echo < answer
    rows = rig.ctx.conn.execute("SELECT origin, status FROM chat_turns").fetchall()
    assert sorted((r["origin"], r["status"]) for r in rows) == [("followup_tap", "done"), ("typed", "done")]


async def test_a_tap_whose_label_does_not_match_the_view_is_refused(rig: TgRig) -> None:
    backend = wire(rig.ctx, FakeBackend(deltas(ANSWER)))
    await rig.say("q")
    final = rig.sent("sendRichMessage")[-1]
    token = rig.token_of(final, "Compare with OpenTofu")
    answer_mid = rig.message_id_of(-1, "sendRichMessage")
    rig.ctx.conn.execute("UPDATE tg_views SET state_json = ? WHERE message_id = ?",
                         (json.dumps({"turn_id": 1, "followups": []}), answer_mid))  # fmt: skip
    calls = len(backend.calls)
    await rig.tap(token, message=answer_mid)
    assert len(backend.calls) == calls
    assert "no longer matches" in json.dumps(rig.req.last("answerCallbackQuery"))
