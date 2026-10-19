"""§14.18.4 delta 5: ``⬇️ Show card`` re-sends every open card of the contribution; an armed sibling
comes back at S1."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ossbot.gatekeeper.context import AppContext
from tests.conftest import insert_action
from tests.telegram.reviewkit import card_of, labels, newest_approval, open_card, publish, status, toast, token
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


async def test_show_card_resends_both_and_the_armed_claim_returns_to_s1(rig: TgRig) -> None:
    ctx = rig.ctx
    pr = await open_card(rig)
    claim = await publish(rig, insert_action(ctx, pr.cid, "issue_claim", {"target_number": 101, "body": "Mine."}))
    await rig.tap(token(rig, claim.message_id, "✅ Approve"), message=claim.message_id)
    assert status(ctx, claim.approval) == "armed"
    await rig.tap(token(rig, pr.message_id, "🧭 Explore"), message=pr.message_id)
    explorer = int(ctx.conn.execute("SELECT message_id FROM tg_views WHERE kind = 'explorer'").fetchone()[0])
    await rig.tap(token(rig, explorer, "⬇️ Show card"), message=explorer)
    assert toast(rig) == "Re-sent 2 cards"
    assert status(ctx, claim.approval) == "invalidated" and status(ctx, pr.approval) == "invalidated"
    new_claim = card_of(ctx, newest_approval(ctx, claim.action))
    assert status(ctx, new_claim.approval) == "pending"
    assert labels(rig, new_claim.message_id)[0] == "✅ Approve & Post"
