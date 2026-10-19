"""Card loudness (DESIGN §14.11.13; AC 11 last sentence, F009-R1-6): at most ``loud_cards_per_hour`` new
cards per hour ring; claims and 👤 cards always ring; re-sends (Show card) and autonomous proposals are
silent; an hour later a card rings again."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from ossbot.common.clock import FakeClock
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from tests.conftest import insert_action, packaged_contribution
from tests.telegram.reviewkit import Card, card_of, newest_approval, publish
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def rang(ctx: AppContext, approval: str) -> bool:
    row = db.one(ctx.conn, "SELECT payload_json, priority FROM tg_outbox WHERE dedupe_key = ?", (f"card:{approval}",))
    assert row is not None
    loud = not json.loads(row["payload_json"]).get("disable_notification")
    assert (row["priority"] == 1) is loud  # the cap counts priority-1 card rows
    return loud


async def _pr(rig: TgRig, *, user: bool = False) -> Card:
    cid, action = packaged_contribution(rig.ctx)
    if user:
        with db.transaction(rig.ctx.conn):
            rig.ctx.conn.execute("UPDATE contributions SET source = 'user' WHERE id = ?", (cid,))
    return await publish(rig, action)


async def test_the_hourly_cap(rig: TgRig, clock: FakeClock) -> None:
    ctx = rig.ctx
    cap = ctx.config.telegram.loud_cards_per_hour
    assert cap == 2
    cards = [await _pr(rig) for _ in range(4)]
    assert [rang(ctx, c.approval) for c in cards] == [True, True, False, False]
    claim_cid, _ = packaged_contribution(ctx)
    claim = await publish(rig, insert_action(ctx, claim_cid, "issue_claim", {"target_number": 5, "body": "Hi"}))
    assert rang(ctx, claim.approval)  # class 1: past the cap
    assert rang(ctx, (await _pr(rig, user=True)).approval)  # 👤: past the cap
    # a re-send on request (Show card, /queue → Open, /approve) is silent
    first = cards[0]
    ctx.reissue(first.cid, "tg:4242", keep_expiry=True)
    await rig.flush()
    again = newest_approval(ctx, first.action)
    assert again != first.approval and not rang(ctx, card_of(ctx, again).approval)
    clock.advance(minutes=61)
    assert rang(ctx, (await _pr(rig)).approval)


async def test_cap_zero_never_rings_but_claims_still_do(rig: TgRig) -> None:
    ctx = rig.ctx
    ctx.config.telegram.__dict__["loud_cards_per_hour"] = 0  # as if config/telegram.yaml said 0 (frozen model)
    assert not rang(ctx, (await _pr(rig)).approval)
    cid, _ = packaged_contribution(ctx)
    claim = await publish(rig, insert_action(ctx, cid, "issue_claim", {"target_number": 6, "body": "Hi"}))
    assert rang(ctx, claim.approval)
