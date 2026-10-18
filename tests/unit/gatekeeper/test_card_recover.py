"""Delta 8 (DESIGN §14.18.4.4, AC 31): CardPublisher.recover heals every crash window of card
delivery for a_, p_ and s_ cards; housekeeping reaches it through ctx.card_recover."""

from __future__ import annotations

from typing import Any

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.app import housekeeping_once
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.outbox import OutboxWorker
from ossbot.gatekeeper.telegram.bot import DECISION_TABLES, CardPublisher
from tests.conftest import packaged_contribution
from tests.unit.gatekeeper.decision_helpers import (
    CHAT,
    RecordingSender,
    install_sources,
    proposed_contribution,
)

CRASH_POINTS = ["after_card_mark_sent", "after_card_sent", "after_last_part_mark_sent", "inside_maybe_arm"]


class Crash(Exception):
    pass


def _new_card(ctx: AppContext, family: str) -> str:
    install_sources(ctx)
    if family == "a":
        _, action = packaged_contribution(ctx)
        return ctx.create_approval(action, "system")
    if family == "p":
        return ctx.decisions.proposals.create(proposed_contribution(ctx), 1, tg_chat_id=CHAT)
    return ctx.decisions.skills.create("sr_1", "git-helper", 1, tg_chat_id=CHAT, body_parts_total=2)


def _row(ctx: AppContext, aid: str) -> Any:
    return db.one(ctx.conn, f"SELECT * FROM {DECISION_TABLES[aid[:2]]} WHERE id = ?", (aid,))


def _skill_parts(publisher: CardPublisher) -> None:
    """Skill renderer stand-in with two parts (the real one is PLAN item 36)."""
    from ossbot.gatekeeper.telegram.cards import CardMessages

    publisher.ctx.decisions.skills.renderer = lambda row: CardMessages("🧩 Skill card", ["part 1", "part 2"])


async def _drain(worker: OutboxWorker, rounds: int = 40) -> None:
    for _ in range(rounds):
        await worker.run_once()


@pytest.mark.parametrize("family", ["a", "p", "s"])
@pytest.mark.parametrize("point", CRASH_POINTS)
async def test_crash_then_recover(ctx: AppContext, family: str, point: str) -> None:
    publisher = CardPublisher(ctx)
    _skill_parts(publisher)
    ctx.on_approval_created = publisher.publish
    sender = RecordingSender()
    aid = _new_card(ctx, family)
    publisher.publish(aid)
    real_card_sent, real_maybe_arm = publisher._card_sent, publisher._maybe_arm
    total = publisher.ctx.decisions.for_id(aid).part_count(aid)

    async def crashing_on_sent(row: Any, result: dict[str, Any]) -> None:
        key = row.dedupe_key or ""
        if point == "after_card_mark_sent" and key.startswith("card:"):
            raise Crash
        if point == "after_card_sent" and key.startswith("card:"):
            real_card_sent(aid, int(result["message_id"]))
            raise Crash
        if point == "after_last_part_mark_sent" and row.part_index == total - 1:
            raise Crash
        await publisher.on_sent(row, result)

    def crashing_arm(approval_id: str) -> None:
        if point == "inside_maybe_arm":
            raise Crash
        real_maybe_arm(approval_id)

    if point == "inside_maybe_arm":
        publisher._maybe_arm = crashing_arm  # type: ignore[method-assign]
    worker = OutboxWorker(ctx.outbox, sender, crashing_on_sent, batch=1)
    await _drain(worker)
    # restart: a fresh publisher (the old in-memory state is gone), recover, keep delivering
    publisher._maybe_arm = real_maybe_arm  # type: ignore[method-assign]
    fresh = CardPublisher(ctx)
    _skill_parts(fresh)
    ctx.on_approval_created = fresh.publish
    fresh.recover()
    await _drain(OutboxWorker(ctx.outbox, sender, fresh.on_sent, batch=1))
    row = _row(ctx, aid)
    assert row["tg_message_id"] is not None
    keys = [k for (k,) in ctx.conn.execute("SELECT dedupe_key FROM tg_outbox WHERE status = 'sent'")]
    assert keys.count(f"card:{aid}") == 1 and keys.count(f"arm:{aid}") == 1  # Approve minted once
    for i in range(total):
        assert keys.count(f"part:{aid}:{i}") == 1  # every part sent once
    if family != "p":
        sent_parts = sum(1 for k in keys if k.startswith(f"part:{aid}:"))
        assert row["body_parts_delivered"] <= sent_parts and row["body_parts_delivered"] == row["body_parts_total"]
    approve = {"a": "approve", "p": "proposal_start", "s": "skill_approve"}[family]
    minted = ctx.conn.execute("SELECT COUNT(*) FROM ui_tokens WHERE approval_id = ? AND purpose = ?", (aid, approve))
    assert minted.fetchone()[0] == 1


async def test_housekeeping_reaches_the_publisher(ctx: AppContext) -> None:
    publisher = CardPublisher(ctx)
    ctx.on_approval_created = publisher.publish
    calls: list[int] = []

    def counting() -> None:
        calls.append(1)
        publisher.recover()

    ctx.card_recover = counting
    aid = _new_card(ctx, "a")  # created, card never enqueued (failed hook after a commit)
    await housekeeping_once(ctx)
    assert db.one(ctx.conn, "SELECT 1 FROM tg_outbox WHERE dedupe_key = ?", (f"card:{aid}",)) is not None
    assert len(calls) == 1  # BA-4: exactly once per tick


async def test_a_failing_card_recover_does_not_stop_the_expiry_sweep(
    ctx: AppContext, caplog: pytest.LogCaptureFixture
) -> None:
    swept: list[int] = []
    real_sweep = ctx.state_machine.sweep_expired

    def sweep() -> None:
        swept.append(1)
        real_sweep()

    def broken() -> None:
        raise RuntimeError("render failed")

    ctx.state_machine.sweep_expired = sweep  # type: ignore[method-assign]
    ctx.card_recover = broken
    await housekeeping_once(ctx)
    await housekeeping_once(ctx)
    assert len(swept) == 2
    assert [r for r in caplog.records if getattr(r, "event", None) == "card_recover_failed"]


async def test_pre_migration_card_rows_are_skipped(ctx: AppContext, caplog: pytest.LogCaptureFixture) -> None:
    publisher = CardPublisher(ctx)
    aid = _new_card(ctx, "a")
    publisher.publish(aid)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE tg_outbox SET status = 'sent' WHERE dedupe_key = ?", (f"card:{aid}",))
    publisher.recover()
    assert _row(ctx, aid)["tg_message_id"] is None
    assert any("result_message_id" in r.message for r in caplog.records)
