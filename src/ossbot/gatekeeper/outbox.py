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
from datetime import datetime, timedelta
from typing import Any, Protocol

from ossbot.common.clock import Clock, to_iso
from ossbot.gatekeeper import db
from ossbot.gatekeeper.telegram.pending_input import PendingInputs

log = logging.getLogger(__name__)
MAX_BACKOFF = timedelta(minutes=10)
MAX_ATTEMPTS = 50
PRIORITIES = (0, 1, 2, 3)  # P0 safety (always loud) .. P3 background (§14.13.1)
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
    thread_key: str | None = None  # system topic name, resolved to message_thread_id at send time
    priority: int = 2
    has_buttons: bool = False


OnSent = Callable[[OutboxRow, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class HoldRequest:
    """What ``Outbox.hold_policy`` (``telegram.notify.Notifier.decide``) sees of a row being queued."""

    priority: int
    has_buttons: bool  # tokens to bind, an approval id or an inline keyboard
    thread_key: str | None
    category: str | None = None  # a /notify category (§14.13.2)
    # True only for P2 FYIs (``AppContext.notify``, feed notices): command replies, views and the
    # security FYI are never batched (§14.13.1)
    holdable: bool = False


@dataclass(frozen=True)
class HoldDecision:
    hold_until: datetime | None = None  # status 'held' until then (P2 without buttons only)
    silent: bool | None = None  # sets disable_notification on send* rows; None = leave as queued


def _no_hold(req: HoldRequest) -> HoldDecision:
    return HoldDecision()


def _has_buttons(payload: Mapping[str, Any]) -> bool:
    markup = payload.get("reply_markup")
    return isinstance(markup, dict) and bool(markup.get("inline_keyboard"))


class Outbox:
    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        self.conn = conn
        self.clock = clock
        # Set by telegram.notify (priorities, quiet hours, batching, §14.13). Whatever it answers, rows
        # with buttons, tokens or an approval and every P0/P1 row are never held (§14.13.3, AC 49).
        self.hold_policy: Callable[[HoldRequest], HoldDecision] = _no_hold

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
        priority: int = 2,
        thread_key: str | None = None,
        coalesce_key: str | None = None,
        category: str | None = None,
        holdable: bool = False,
    ) -> int | None:
        """``bind_tokens`` (minted with ``tg_message_id = NULL``) and ``view`` (the ``tg_views`` row)
        are applied by ``mark_sent`` with the sent message's id ("mint then bind", §14.18.2). The view
        kind is checked here, so a bad kind fails before anything is sent. ``priority`` is P0..P3
        (§14.13.1); ``thread_key`` names the system topic, resolved when the row is sent."""
        if view is not None and view.get("kind") not in VIEW_KINDS:
            raise ValueError(f"unknown view kind {view.get('kind')!r}")
        if priority not in PRIORITIES:
            raise ValueError(f"bad priority {priority!r}")
        buttons = bool(bind_tokens) or approval_id is not None or _has_buttons(payload)
        decision = self.hold_policy(HoldRequest(priority, buttons, thread_key, category, holdable))
        hold = decision.hold_until if (priority >= 2 and not buttons and holdable) else None
        if decision.silent is not None and method.startswith("send"):
            payload = {**payload, "disable_notification": decision.silent}
            if isinstance(payload.get("_classic"), dict):
                payload["_classic"] = {**payload["_classic"], "disable_notification": decision.silent}
        status = "held" if hold is not None else "pending"
        due = hold if hold is not None else self.clock.now()
        with db.transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO tg_outbox (method, payload_json, dedupe_key, approval_id, part_index, status,"
                " next_attempt_at, bind_tokens_json, view_json, priority, thread_key, coalesce_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(dedupe_key) DO NOTHING",
                (method, json.dumps(payload, ensure_ascii=False), dedupe_key, approval_id, part_index, status,
                 to_iso(due), json.dumps(bind_tokens) if bind_tokens else None,
                 json.dumps(dict(view), sort_keys=True) if view is not None else None, priority, thread_key,
                 coalesce_key),
            )  # fmt: skip
            return cur.lastrowid if cur.rowcount else None

    def due(self, limit: int = 20) -> list[OutboxRow]:
        """Due rows, P0 first, then by id (P0 alerts never wait behind feed messages, §14.13.1)."""
        rows = self.conn.execute(
            "SELECT * FROM tg_outbox WHERE status = 'pending' AND next_attempt_at <= ?"
            " ORDER BY CASE WHEN priority = 0 THEN 0 ELSE 1 END, id LIMIT ?",
            (to_iso(self.clock.now()), limit),
        ).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(r: sqlite3.Row) -> OutboxRow:
        payload = json.loads(r["payload_json"])
        return OutboxRow(
            r["id"], r["method"], payload, r["approval_id"], r["part_index"], r["attempts"], r["dedupe_key"],
            r["thread_key"], int(r["priority"]),
            bool(r["bind_tokens_json"]) or r["approval_id"] is not None or _has_buttons(payload),
        )  # fmt: skip

    def has_due_urgent(self) -> bool:
        """A due P0/P1 row (the dashboard skips its edit meanwhile, §14.10.3)."""
        return db.one(
            self.conn, "SELECT 1 FROM tg_outbox WHERE status = 'pending' AND priority <= 1 AND next_attempt_at <= ?"
            " LIMIT 1", (to_iso(self.clock.now()),),
        ) is not None  # fmt: skip

    def requeue(self, row: OutboxRow, method: str, payload: dict[str, Any], *, thread_key: str | None = None) -> None:
        """Re-send a row with another method/payload (rich -> classic fallback, flat-mode resend)."""
        with db.transaction(self.conn):
            self.conn.execute(
                "UPDATE tg_outbox SET method = ?, payload_json = ?, thread_key = ?, status = 'pending',"
                " next_attempt_at = ?, last_error = NULL WHERE id = ?",
                (method, json.dumps(payload, ensure_ascii=False), thread_key, to_iso(self.clock.now()), row.id),
            )

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
        prompt = (view.get("state") or {}).get("prompt")
        if isinstance(prompt, dict):  # a ForceReply prompt: correlate replies by this message id (§14.10.4 rule 3)
            fields = {k_: v for k_, v in prompt.items() if k_ != "kind"}
            PendingInputs(self.conn, self.clock).open(prompt["kind"], chat_id=int(payload.get("chat_id", 0)),
                                                      prompt_message_id=message_id, **fields)  # fmt: skip

    def mark_retry(self, row: OutboxRow, error: SendError) -> str:
        """Returns the new status (``failed`` when permanent or out of attempts)."""
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
        return status


# (row, error) -> True when the hook recovered the row (re-queued it), else the row stays failed
OnFailed = Callable[[OutboxRow, SendError], Awaitable[bool]]
# (row) -> the row to send (thread resolution, flat-mode section icon), or None to send as is
Prepare = Callable[[OutboxRow], OutboxRow]


class OutboxWorker:
    """``on_sent`` sees each delivered row with the Bot API result (card message ids, §14.18.4.4);
    ``on_failed`` sees permanent failures (rich -> classic fallback, "thread not found", a deleted
    dashboard, §14.20.5); ``prepare`` resolves the row's topic just before the send."""

    def __init__(self, outbox: Outbox, sender: Sender, on_sent: OnSent | None = None, batch: int = 20, *,
                 on_failed: OnFailed | None = None, prepare: Prepare | None = None) -> None:  # fmt: skip
        self.outbox = outbox
        self.sender = sender
        self.on_sent = on_sent
        self.batch = batch
        self.on_failed = on_failed
        self.prepare = prepare

    async def run_once(self) -> int:
        sent = 0
        for queued in self.outbox.due(self.batch):
            row = self.prepare(queued) if self.prepare is not None else queued
            try:
                result = await self.sender.send(row.method, row.payload)
            except SendError as exc:
                log.warning("outbox send failed: %s", exc, extra={"event": "outbox_retry"})
                status = self.outbox.mark_retry(row, exc)
                if status == "failed" and self.on_failed is not None:
                    try:
                        await self.on_failed(row, exc)
                    except Exception:
                        log.exception("outbox on_failed hook failed", extra={"event": "outbox_hook_failed"})
                continue
            self.outbox.mark_sent(row, result)
            sent += 1
            if self.on_sent is not None:
                try:
                    await self.on_sent(row, result)
                except Exception:
                    log.exception("outbox on_sent hook failed", extra={"event": "outbox_hook_failed"})
        return sent
