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
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from ossbot.common.clock import Clock, to_iso
from ossbot.gatekeeper import db

log = logging.getLogger(__name__)
MAX_BACKOFF = timedelta(minutes=10)
MAX_ATTEMPTS = 50


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
    ) -> int | None:
        with db.transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO tg_outbox (method, payload_json, dedupe_key, approval_id, part_index, status,"
                " next_attempt_at) VALUES (?, ?, ?, ?, ?, 'pending', ?) ON CONFLICT(dedupe_key) DO NOTHING",
                (method, json.dumps(payload, ensure_ascii=False), dedupe_key, approval_id, part_index,
                 to_iso(self.clock.now())),
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
            )
            for r in rows
        ]

    def mark_sent(self, row: OutboxRow) -> None:
        with db.transaction(self.conn):
            self.conn.execute("UPDATE tg_outbox SET status = 'sent', last_error = NULL WHERE id = ?", (row.id,))
            if row.approval_id is not None and row.part_index is not None:
                self.conn.execute(
                    "UPDATE approvals SET body_parts_delivered = MIN(body_parts_total, body_parts_delivered + 1)"
                    " WHERE id = ?",
                    (row.approval_id,),
                )

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
    def __init__(self, outbox: Outbox, sender: Sender) -> None:
        self.outbox = outbox
        self.sender = sender

    async def run_once(self) -> int:
        sent = 0
        for row in self.outbox.due():
            try:
                await self.sender.send(row.method, row.payload)
            except SendError as exc:
                log.warning("outbox send failed: %s", exc, extra={"event": "outbox_retry"})
                self.outbox.mark_retry(row, exc)
                continue
            self.outbox.mark_sent(row)
            sent += 1
        return sent
