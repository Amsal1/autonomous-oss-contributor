"""Persistent Telegram outbox (``tg_outbox``, DESIGN §12.7, §19).

Messages are enqueued in the same transaction as their cause where possible and delivered by
``OutboxWorker`` through a pluggable ``Sender`` (the real Telegram sender lands in FEAT-002).
429 waits ``retry_after``; network errors back off up to 10 minutes; a delivered approval body
part advances ``approvals.body_parts_delivered`` (the Approve button needs every part).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from ossbot.common.clock import Clock, to_iso
from ossbot.gatekeeper import db

log = logging.getLogger(__name__)
MAX_BACKOFF = timedelta(minutes=10)
MAX_ATTEMPTS = 50
# Mirrors the tg_views.kind CHECK of 0002_decisions.sql (DESIGN §14.20.2).
VIEW_KINDS: frozenset[str] = frozenset({
    "dashboard", "card", "explorer", "file", "interdiff", "draft_review", "status", "digest", "feature", "picker",
    "progress", "chat_answer", "body_part", "commit_msg", "fyi", "confirm_prompt", "proposal", "proposal_details",
    "rescope", "ceiling", "work_request", "link_card", "skill_card", "skill_part",
})  # fmt: skip
# Card parts are counted on the table their approval id names (proposal cards have no parts).
PART_TABLES: dict[str, str] = {"a_": "approvals", "s_": "skill_approvals"}


def thread_key(payload: Mapping[str, Any]) -> str:
    """``<chat_id>:<message_thread_id or 0>`` for ``tg_thread_seq`` (§14.11.3)."""
    return f"{payload.get('chat_id', 0)}:{payload.get('message_thread_id') or 0}"


class SendError(Exception):
    def __init__(self, message: str, *, retry_after: float | None = None, permanent: bool = False) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.permanent = permanent


class Sender(Protocol):
    async def send(self, method: str, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class OutboxRow:
    id: int
    method: str
    payload: dict[str, Any]
    approval_id: str | None
    part_index: int | None
    attempts: int
    dedupe_key: str | None = None


OnSent = Callable[[OutboxRow, dict[str, Any]], Awaitable[None]]


class Outbox:
    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        self.conn = conn
        self.clock = clock

    def enqueue(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        approval_id: str | None = None,
        part_index: int | None = None,
        bind_tokens: list[str] | None = None,
        view: Mapping[str, Any] | None = None,
    ) -> int | None:
        """``bind_tokens`` (minted with ``tg_message_id = NULL``) and ``view`` (the ``tg_views`` row)
        are applied by ``mark_sent`` with the sent message's id ("mint then bind", §14.18.2). The view
        kind is checked here, so a bad kind fails before anything is sent."""
        if view is not None and view.get("kind") not in VIEW_KINDS:
            raise ValueError(f"unknown view kind {view.get('kind')!r}")
        with db.transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO tg_outbox (method, payload_json, dedupe_key, approval_id, part_index, status,"
                " next_attempt_at, bind_tokens_json, view_json) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)"
                " ON CONFLICT(dedupe_key) DO NOTHING",
                (method, json.dumps(payload, ensure_ascii=False), dedupe_key, approval_id, part_index,
                 to_iso(self.clock.now()), json.dumps(bind_tokens) if bind_tokens else None,
                 json.dumps(dict(view), sort_keys=True) if view is not None else None),
            )  # fmt: skip
            return cur.lastrowid if cur.rowcount else None

    def due(self, limit: int = 20) -> list[OutboxRow]:
        rows = self.conn.execute(
            "SELECT * FROM tg_outbox WHERE status = 'pending' AND next_attempt_at <= ? ORDER BY id LIMIT ?",
            (to_iso(self.clock.now()), limit),
        ).fetchall()
        return [
            OutboxRow(
                r["id"],
                r["method"],
                json.loads(r["payload_json"]),
                r["approval_id"],
                r["part_index"],
                r["attempts"],
                r["dedupe_key"],
            )
            for r in rows
        ]

    def mark_sent(self, row: OutboxRow, result: Mapping[str, Any] | None = None) -> None:
        """One transaction (delta 6, §14.18.2 step 3): the row is sent with its result message id,
        the kit tokens it carries are bound to that message, its ``tg_views`` row is written, the
        thread counter moves for ``send*`` methods, and a delivered card part is counted (by id
        prefix: ``a_`` approvals, ``s_`` skill approvals). Part counting happens only here."""
        mid = (result or {}).get("message_id")
        message_id = mid if isinstance(mid, int) and not isinstance(mid, bool) else None
        with db.transaction(self.conn):
            self.conn.execute(
                "UPDATE tg_outbox SET status = 'sent', last_error = NULL, result_message_id = ? WHERE id = ?",
                (message_id, row.id),
            )
            stored = db.one(self.conn, "SELECT bind_tokens_json, view_json FROM tg_outbox WHERE id = ?", (row.id,))
            if message_id is not None and stored is not None:
                if stored["bind_tokens_json"]:
                    tokens = [str(t) for t in json.loads(stored["bind_tokens_json"])]
                    marks = ",".join("?" * len(tokens))
                    self.conn.execute(
                        f"UPDATE ui_tokens SET tg_message_id = ? WHERE token IN ({marks}) AND tg_message_id IS NULL",  # noqa: S608
                        (message_id, *tokens),
                    )
                if stored["view_json"]:
                    self._write_view(message_id, row.payload, json.loads(stored["view_json"]))
            if row.method.startswith("send"):
                self.conn.execute(
                    "INSERT INTO tg_thread_seq (thread_key, n) VALUES (?, 1)"
                    " ON CONFLICT(thread_key) DO UPDATE SET n = n + 1",
                    (thread_key(row.payload),),
                )
            if row.approval_id is not None and row.part_index is not None:
                table = PART_TABLES.get(row.approval_id[:2])
                if table is not None:
                    self.conn.execute(
                        f"UPDATE {table} SET body_parts_delivered = MIN(body_parts_total, body_parts_delivered + 1)"  # noqa: S608
                        " WHERE id = ?",
                        (row.approval_id,),
                    )

    def _write_view(self, message_id: int, payload: Mapping[str, Any], view: Mapping[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO tg_views (message_id, chat_id, thread_id, kind, approval_id, cid, rev, action_id, path, page,"
            " state_json, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET"
            " kind = excluded.kind, approval_id = excluded.approval_id, cid = excluded.cid, rev = excluded.rev,"
            " action_id = excluded.action_id, path = excluded.path, page = excluded.page,"
            " state_json = excluded.state_json, updated_at = excluded.updated_at, stale = 0",
            (
                message_id, int(payload.get("chat_id", 0)), payload.get("message_thread_id"), view["kind"],
                view.get("approval_id"), view.get("cid"), view.get("rev"), view.get("action_id"), view.get("path"),
                view.get("page"), json.dumps(view.get("state", {}), sort_keys=True), to_iso(self.clock.now()),
            ),
        )  # fmt: skip

    def mark_retry(self, row: OutboxRow, error: SendError) -> None:
        attempts = row.attempts + 1
        if error.permanent or attempts >= MAX_ATTEMPTS:
            status, delay = "failed", timedelta(0)
        else:
            status = "pending"
            delay = (
                timedelta(seconds=error.retry_after)
                if error.retry_after is not None
                else min(MAX_BACKOFF, timedelta(seconds=5 * 2 ** min(attempts, 10)))
            )
        with db.transaction(self.conn):
            self.conn.execute(
                "UPDATE tg_outbox SET status = ?, attempts = ?, next_attempt_at = ?, last_error = ? WHERE id = ?",
                (status, attempts, to_iso(self.clock.now() + delay), str(error)[:500], row.id),
            )


class OutboxWorker:
    """``on_sent`` sees each delivered row with the Bot API result (card message ids, §14.4)."""

    def __init__(self, outbox: Outbox, sender: Sender, on_sent: OnSent | None = None, batch: int = 20) -> None:
        self.outbox = outbox
        self.sender = sender
        self.on_sent = on_sent
        self.batch = batch

    async def run_once(self) -> int:
        sent = 0
        for row in self.outbox.due(self.batch):
            try:
                result = await self.sender.send(row.method, row.payload)
            except SendError as exc:
                log.warning("outbox send failed: %s", exc, extra={"event": "outbox_retry"})
                self.outbox.mark_retry(row, exc)
                continue
            self.outbox.mark_sent(row, result)
            sent += 1
            if self.on_sent is not None:
                try:
                    await self.on_sent(row, result)
                except Exception:
                    log.exception("outbox on_sent hook failed", extra={"event": "outbox_hook_failed"})
        return sent
