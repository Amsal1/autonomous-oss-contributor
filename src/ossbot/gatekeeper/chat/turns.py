"""Chat turns and session keys (DESIGN §14.12, §14.18.5, §14.20.2; CR3-BA-15, CR3-AC-59/67).

A turn is a ``chat_turns`` row opened before the Hermes call (``status='streaming'``) and closed by the
task's one done callback: ``finalize_turn`` sets ``stopped`` for a cancelled task and ``failed`` for a
raised one; a turn that finished normally has already set ``done``, so the ``WHERE status =
'streaming'`` makes the callback a no-op. ``reset_streaming`` closes rows a crash left open. The
answer is stored only after redaction (U14). Flat-mode session keys are ``tg-<uid>-<n>``; ``/new``
bumps ``n`` (``chat_sessions``) while Hermes memory persists.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from typing import Literal

from ossbot.common.clock import Clock, to_iso
from ossbot.gatekeeper import db
from ossbot.gatekeeper.telegram.ui.redact import redact

ANSWER_MAX = 200 * 1024
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
             answer_message_id: int | None = None) -> str:  # fmt: skip
        """Close a finished turn; the answer is redacted before it is stored. Returns the stored text."""
        text = redact(answer[:ANSWER_MAX])[0]
        with db.transaction(self.conn):
            self.conn.execute(
                "UPDATE chat_turns SET status = 'done', finished_at = ?, answer_text = ?, model = ?, tools_json = ?,"
                " run_id = ?, answer_message_id = ? WHERE turn_id = ? AND status = 'streaming'",
                (to_iso(self.clock.now()), text, model, json.dumps([t[:64] for t in tools][:64]), run_id,
                 answer_message_id, turn_id),
            )  # fmt: skip
        return text

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
            cur = self.conn.execute(
                "UPDATE chat_turns SET status = 'failed', finished_at = ? WHERE status = 'streaming'",
                (to_iso(self.clock.now()),),
            )
        return cur.rowcount

    def status(self, turn_id: int) -> str | None:
        row = db.one(self.conn, "SELECT status FROM chat_turns WHERE turn_id = ?", (turn_id,))
        return None if row is None else str(row["status"])
