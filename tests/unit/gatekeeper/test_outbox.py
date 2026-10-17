"""Persistent outbox with a pluggable sender: retries, 429 retry_after, body-part delivery."""

from __future__ import annotations

from typing import Any

from ossbot.common.clock import FakeClock
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.outbox import OutboxWorker, SendError
from tests.conftest import packaged_contribution


class FlakySender:
    def __init__(self, errors: list[SendError]) -> None:
        self.errors = errors
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def send(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append((method, payload))
        return {"ok": True}


async def test_retry_after_and_delivery(ctx: AppContext, clock: FakeClock) -> None:
    ctx.outbox.enqueue("sendMessage", {"chat_id": 4242, "text": "hi"}, dedupe_key="k1")
    assert ctx.outbox.enqueue("sendMessage", {"chat_id": 4242, "text": "dup"}, dedupe_key="k1") is None
    sender = FlakySender([SendError("429", retry_after=30)])
    worker = OutboxWorker(ctx.outbox, sender)
    assert await worker.run_once() == 0
    assert await worker.run_once() == 0  # not due yet
    clock.advance(seconds=31)
    assert await worker.run_once() == 1
    assert sender.sent == [("sendMessage", {"chat_id": 4242, "text": "hi"})]


async def test_permanent_error_marks_failed(ctx: AppContext) -> None:
    ctx.outbox.enqueue("sendMessage", {"chat_id": 4242, "text": "<b"})
    await OutboxWorker(ctx.outbox, FlakySender([SendError("400 can't parse entities", permanent=True)])).run_once()
    row = ctx.conn.execute("SELECT status, attempts FROM tg_outbox").fetchone()
    assert row["status"] == "failed" and row["attempts"] == 1


async def test_body_parts_delivered_counted(ctx: AppContext) -> None:
    _, action = packaged_contribution(ctx)
    approval = ctx.approvals.create(action, tg_chat_id=4242, body_parts_total=2)
    for part in range(2):
        ctx.outbox.enqueue(
            "sendMessage", {"chat_id": 4242, "text": f"part {part}"}, approval_id=approval, part_index=part
        )
    await OutboxWorker(ctx.outbox, FlakySender([])).run_once()
    row = db.one(ctx.conn, "SELECT body_parts_delivered FROM approvals WHERE id = ?", (approval,))
    assert row is not None and row[0] == 2


def test_notify_dropped_without_telegram_user(ctx: AppContext) -> None:
    profile = ctx.paths.config_dir / "profile.yaml"
    profile.write_text(profile.read_text().replace("user_id: 4242", "user_id: 0"))
    ctx.snapshots.apply_reload(ctx.snapshots.prepare_reload().hash, "uid:1000/operator")
    before = ctx.conn.execute("SELECT COUNT(*) FROM tg_outbox").fetchone()[0]
    ctx.notify("hello")
    assert ctx.conn.execute("SELECT COUNT(*) FROM tg_outbox").fetchone()[0] == before
