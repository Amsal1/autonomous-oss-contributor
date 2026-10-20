"""Chat turns and session keys (DESIGN §14.12, §14.18.5, §14.20.2; CR3-BA-15, CR3-AC-59/67).

A turn is a ``chat_turns`` row opened before the Hermes call (``status='streaming'``) and closed by the
task's one done callback: ``finalize_turn`` sets ``stopped`` for a cancelled task and ``failed`` for a
raised one; a turn that finished normally has already set ``done``, so the ``WHERE status =
'streaming'`` makes the callback a no-op. ``reset_streaming`` closes rows a crash left open. The
answer is stored only after redaction (U14). Flat-mode session keys are ``tg-<uid>-<n>``; ``/new``
bumps ``n`` (``chat_sessions``) while Hermes memory persists.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sqlite3
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Literal

from ossbot.common.clock import Clock, to_iso
from ossbot.gatekeeper import db
from ossbot.gatekeeper.telegram.ui.redact import redact

log = logging.getLogger(__name__)
ANSWER_MAX = 200 * 1024
U12_STREAMING_MAX = timedelta(minutes=30)  # U12: a streaming row counts this long at most
U12_FINISHED_GRACE = timedelta(minutes=10)  # U12: a finished turn still takes cards and notices this long
Origin = Literal["typed", "followup_tap", "media", "retry"]
Final = Literal["done", "stopped", "failed"]


def new_turn_id() -> int:
    return secrets.randbits(62) or 1  # a positive int63: also the Bot API draft_id


class TurnStore:
    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        self.conn, self.clock = conn, clock

    # ---------------------------------------------------------------- sessions
    def session_key(self, tg_user_id: int) -> str:
        row = db.one(self.conn, "SELECT n FROM chat_sessions WHERE tg_user_id = ?", (tg_user_id,))
        return f"tg-{tg_user_id}-{int(row['n']) if row else 1}"

    def new_session(self, tg_user_id: int) -> str:
        now = to_iso(self.clock.now())
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO chat_sessions (tg_user_id, n, updated_at) VALUES (?, 2, ?) ON CONFLICT(tg_user_id)"
                " DO UPDATE SET n = n + 1, updated_at = excluded.updated_at", (tg_user_id, now))  # fmt: skip
        return self.session_key(tg_user_id)

    # ---------------------------------------------------------------- turns
    def start(self, session_key: str, user_message_id: int, *, thread_id: int | None = None, origin: Origin = "typed",
              had_attachments: bool = False) -> int:  # fmt: skip
        turn_id = new_turn_id()
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO chat_turns (turn_id, session_key, thread_id, user_message_id, status, started_at, origin,"
                " had_attachments) VALUES (?, ?, ?, ?, 'streaming', ?, ?, ?)",
                (turn_id, session_key, thread_id, user_message_id, to_iso(self.clock.now()), origin,
                 int(had_attachments)),
            )  # fmt: skip
        return turn_id

    def busy(self, session_key: str) -> bool:
        return db.one(self.conn, "SELECT 1 FROM chat_turns WHERE session_key = ? AND status = 'streaming'",
                      (session_key,)) is not None  # fmt: skip

    def done(self, turn_id: int, answer: str, *, model: str | None, tools: list[str], run_id: str | None = None,
             answer_message_id: int | None = None, status: Literal["done", "stopped"] = "done") -> str:  # fmt: skip
        """Close a turn with its answer; the answer is redacted before it is stored (a stopped or cut
        stream passes its ``draft_visible`` text, §14.12.1 step 4). Returns the stored text."""
        text = redact(answer[:ANSWER_MAX])[0]
        with db.transaction(self.conn):
            self.conn.execute(
                "UPDATE chat_turns SET status = ?, finished_at = ?, answer_text = ?, model = ?, tools_json = ?,"
                " run_id = ?, answer_message_id = ? WHERE turn_id = ? AND status = 'streaming'",
                (status, to_iso(self.clock.now()), text, model, json.dumps([t[:64] for t in tools][:64]), run_id,
                 answer_message_id, turn_id),
            )  # fmt: skip
        return text

    def set_user_text(self, turn_id: int, text: str) -> None:
        """Only for the ``tg-<uid>-reviews`` session (``Use as comment``, rule 7); ≤ 4,000 characters."""
        with db.transaction(self.conn):
            self.conn.execute("UPDATE chat_turns SET user_text = ? WHERE turn_id = ?", (text[:4000], turn_id))

    def row(self, turn_id: int) -> sqlite3.Row | None:
        return db.one(self.conn, "SELECT * FROM chat_turns WHERE turn_id = ?", (turn_id,))

    def count(self, session_key: str) -> int:
        row = db.one(self.conn, "SELECT COUNT(*) AS n FROM chat_turns WHERE session_key = ?", (session_key,))
        return int(row["n"]) if row else 0

    # ---------------------------------------------------------------- U12
    def active(self, session_key: str) -> sqlite3.Row | None:
        """The turn an unsolicited Hermes message may attach to (§14.12.7 U12, BA-22): a ``streaming`` row
        started in the last 30 min or a row finished in the last 10 min; else None (409 no_active_turn)."""
        now = self.clock.now()
        return db.one(
            self.conn,
            "SELECT turn_id, thread_id, session_key FROM chat_turns WHERE session_key = ? AND ((status = 'streaming'"
            " AND started_at >= ?) OR finished_at >= ?) ORDER BY turn_id DESC LIMIT 1",
            (session_key, to_iso(now - U12_STREAMING_MAX), to_iso(now - U12_FINISHED_GRACE)),
        )  # fmt: skip

    def count_feature_card(self, turn_id: int) -> None:
        with db.transaction(self.conn):
            self.conn.execute("UPDATE chat_turns SET feature_cards = feature_cards + 1 WHERE turn_id = ?", (turn_id,))

    def finalize(self, turn_id: int, *, cancelled: bool, failed: bool) -> None:
        """The task's single done callback (synchronous, idempotent)."""
        status: Final = "stopped" if cancelled else ("failed" if failed else "done")
        with db.transaction(self.conn):
            self.conn.execute(
                "UPDATE chat_turns SET status = ?, finished_at = COALESCE(finished_at, ?) WHERE turn_id = ? AND"
                " status = 'streaming'", (status, to_iso(self.clock.now()), turn_id))  # fmt: skip

    def reset_streaming(self) -> int:
        """At start: rows a crash or restart left streaming become failed."""
        with db.transaction(self.conn):
            # finished_at stays NULL: a turn no task survived never makes U12 accept its session (BA-22)
            cur = self.conn.execute("UPDATE chat_turns SET status = 'failed' WHERE status = 'streaming'")
        return cur.rowcount

    def status(self, turn_id: int) -> str | None:
        row = db.one(self.conn, "SELECT status FROM chat_turns WHERE turn_id = ?", (turn_id,))
        return None if row is None else str(row["status"])


def finalize_turn(turns: TurnStore, turn_id: int) -> Callable[[asyncio.Task[Any]], None]:
    """The chat turn's ``on_done`` for ``Handlers.spawn`` (§14.18.5 item 2, BA-15): runs for every
    outcome, a cancel before the first step included; ``stopped`` when cancelled, ``failed`` when it
    raised, a no-op for a turn that already closed its row. A DB error is logged, never raised (the
    row then stays ``streaming`` until the next start's ``reset_streaming``; U12 bounds it by 30 min)."""

    def on_done(task: asyncio.Task[Any]) -> None:
        cancelled = task.cancelled()
        failed = not cancelled and task.exception() is not None
        try:
            turns.finalize(turn_id, cancelled=cancelled, failed=failed)
        except sqlite3.Error:
            log.exception("finalize_turn failed", extra={"event": "finalize_turn_failed"})

    return on_done
