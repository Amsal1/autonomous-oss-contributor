"""Persistent outbox with a pluggable sender: retries, 429 retry_after, body-part delivery."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ossbot.common.clock import FakeClock
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.outbox import OutboxWorker, SendError, merge_view_state
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


class FixedSender:
    """Every call answers message 101 (a send creates it, an edit of it answers the same id)."""

    async def send(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"message_id": 101}


async def _view_row(ctx: AppContext, kind: str, state: dict[str, Any], *, edited: dict[str, Any] | None = None,
                    edit_kind: str | None = None, edit_state: dict[str, Any] | None = None) -> Any:  # fmt: skip
    """Send a view as message 101, optionally overwrite its stored state (out-of-band bookkeeping), then
    deliver an ``editMessageText`` of message 101 with ``edit_kind``/``edit_state``, as ``ui_send`` does."""
    worker = OutboxWorker(ctx.outbox, FixedSender())
    ctx.outbox.enqueue("sendMessage", {"chat_id": 4242, "text": "v"}, view={"kind": kind, "state": state})
    await worker.run_once()
    if edited is not None:
        ctx.conn.execute("UPDATE tg_views SET state_json = ? WHERE message_id = 101", (json.dumps(edited),))
    if edit_kind is not None:
        ctx.outbox.enqueue("editMessageText", {"chat_id": 4242, "message_id": 101, "text": "e"},
                           view={"kind": edit_kind, "state": edit_state or {}})  # fmt: skip
        await worker.run_once()
    return db.one(ctx.conn, "SELECT kind, state_json FROM tg_views WHERE message_id = 101")


async def test_edit_keeps_state_the_view_does_not_set(ctx: AppContext) -> None:
    """The status-card loop (2026-10-22): bookkeeping stored next to the view survives its edit."""
    row = await _view_row(ctx, "status", {}, edited={"polled_at": "2026-10-22T00:00:00Z", "hash": "h"},
                          edit_kind="status")  # fmt: skip
    assert json.loads(row["state_json"]) == {"polled_at": "2026-10-22T00:00:00Z", "hash": "h"}


async def test_edit_view_keys_override(ctx: AppContext) -> None:
    row = await _view_row(ctx, "file", {"a": 1, "b": 2}, edit_kind="file", edit_state={"b": 3})
    assert json.loads(row["state_json"]) == {"a": 1, "b": 3}


async def test_kind_change_replaces_state(ctx: AppContext) -> None:
    row = await _view_row(ctx, "status", {"a": 1}, edit_kind="fyi")
    assert row["kind"] == "fyi" and row["state_json"] == "{}"


async def test_edit_never_reopens_a_prompt_from_old_state(ctx: AppContext) -> None:
    prompt = {"prompt": {"kind": "comment"}}
    row = await _view_row(ctx, "confirm_prompt", prompt, edit_kind="confirm_prompt")
    assert ctx.conn.execute("SELECT COUNT(*) FROM pending_input").fetchone()[0] == 1  # the send's prompt only
    assert json.loads(row["state_json"]) == prompt  # kept as data, not re-opened by the edit


@pytest.mark.parametrize(
    ("old_kind", "old_json", "kind", "new", "want"),
    [
        (None, None, "status", {"a": 1}, {"a": 1}),  # no row: insert
        ("status", "not json", "status", {"a": 1}, {"a": 1}),
        ("status", "[1, 2]", "status", {"a": 1}, {"a": 1}),
        ("status", None, "status", {}, {}),
        ("status", '{"a": {"x": 1}, "b": 2}', "status", {"a": {"y": 2}}, {"a": {"y": 2}, "b": 2}),  # shallow
        ("card", '{"a": 1}', "fyi", {}, {}),
    ],
)
def test_merge_view_state(old_kind: str | None, old_json: str | None, kind: str, new: dict[str, Any],
                          want: dict[str, Any]) -> None:  # fmt: skip
    assert merge_view_state(old_kind, old_json, kind, new) == want


def test_notify_dropped_without_telegram_user(ctx: AppContext) -> None:
    profile = ctx.paths.config_dir / "profile.yaml"
    profile.write_text(profile.read_text().replace("user_id: 4242", "user_id: 0"))
    ctx.snapshots.apply_reload(ctx.snapshots.prepare_reload().hash, "uid:1000/operator")
    before = ctx.conn.execute("SELECT COUNT(*) FROM tg_outbox").fetchone()[0]
    ctx.notify("hello")
    assert ctx.conn.execute("SELECT COUNT(*) FROM tg_outbox").fetchone()[0] == before
