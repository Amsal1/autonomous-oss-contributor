"""ForceReply correlation (DESIGN §14.10.4 rule 3, §14.20.2 ``pending_input``).

A prompt is opened when its ForceReply message is sent (``open`` after the message id is known) and
matched only when a reply's ``reply_to_message.message_id`` equals ``prompt_message_id`` in the owner
chat, before its 15-minute expiry and before it was consumed. ``consume`` is single-use: it marks the
row in the same transaction it reads it, so a replayed reply finds nothing. Anything else falls through
to the next routing rule; nothing here trusts the text of the message being replied to.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from ossbot.common.clock import Clock, to_iso
from ossbot.common.ids import new_token
from ossbot.gatekeeper import db

TTL = timedelta(minutes=15)
InputKind = Literal["changes", "edit_title", "edit_body", "reject_other", "edit_comment", "comment", "line_pick",
                    "review_send_text", "body_file", "feature_fix", "proposal_adjust",
                    "proposal_snooze_days"]  # fmt: skip
INPUT_KINDS: tuple[str, ...] = ("changes", "edit_title", "edit_body", "reject_other", "edit_comment", "comment",
                                "line_pick", "review_send_text", "body_file", "feature_fix", "proposal_adjust",
                                "proposal_snooze_days")  # fmt: skip
COLUMNS = ("cid", "approval_id", "action_id", "proposal_id", "rev", "path", "side", "hunk")


@dataclass(frozen=True)
class Prompt:
    id: str
    kind: InputKind
    chat_id: int
    prompt_message_id: int
    cid: str | None = None
    approval_id: str | None = None
    action_id: str | None = None
    proposal_id: str | None = None
    rev: int | None = None
    path: str | None = None
    side: str | None = None
    hunk: int | None = None
    line_candidates: tuple[Any, ...] = ()


class PendingInputs:
    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        self.conn, self.clock = conn, clock

    def open(self, kind: InputKind, *, chat_id: int, prompt_message_id: int, **fields: Any) -> str:
        if kind not in INPUT_KINDS:
            raise ValueError(f"bad input kind {kind!r}")
        unknown = set(fields) - {*COLUMNS, "line_candidates"}
        if unknown:
            raise ValueError(f"unknown prompt fields {sorted(unknown)}")
        now = self.clock.now()
        pid = new_token()
        cands = fields.pop("line_candidates", None)
        cols = {c: fields.get(c) for c in COLUMNS}
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO pending_input (id, kind, cid, approval_id, action_id, proposal_id, rev, path, side, hunk,"
                " line_candidates_json, prompt_message_id, chat_id, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, kind, *cols.values(), json.dumps(list(cands)) if cands else None, prompt_message_id, chat_id,
                 to_iso(now), to_iso(now + TTL)),
            )  # fmt: skip
        return pid

    def peek(self, chat_id: int, reply_to_message_id: int | None) -> Prompt | None:
        """The open prompt this reply answers, without consuming it."""
        if reply_to_message_id is None:
            return None
        row = db.one(
            self.conn,
            "SELECT * FROM pending_input WHERE chat_id = ? AND prompt_message_id = ? AND consumed_at IS NULL"
            " AND expires_at > ? ORDER BY created_at DESC LIMIT 1",
            (chat_id, reply_to_message_id, to_iso(self.clock.now())),
        )
        return None if row is None else _prompt(row)

    def consume(self, chat_id: int, reply_to_message_id: int | None) -> Prompt | None:
        """Match and consume in one transaction (single consumption)."""
        if reply_to_message_id is None:
            return None
        with db.transaction(self.conn):
            prompt = self.peek(chat_id, reply_to_message_id)
            if prompt is None:
                return None
            cur = self.conn.execute(
                "UPDATE pending_input SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (to_iso(self.clock.now()), prompt.id),
            )
            return prompt if cur.rowcount == 1 else None

    def gc(self) -> int:
        """Drop prompts that ended more than a day ago."""
        cutoff = to_iso(self.clock.now() - timedelta(days=1))
        with db.transaction(self.conn):
            cur = self.conn.execute("DELETE FROM pending_input WHERE expires_at < ?", (cutoff,))
        return cur.rowcount


def _prompt(row: sqlite3.Row) -> Prompt:
    cands = tuple(json.loads(row["line_candidates_json"])) if row["line_candidates_json"] else ()
    return Prompt(row["id"], row["kind"], int(row["chat_id"]), int(row["prompt_message_id"]), row["cid"],
                  row["approval_id"], row["action_id"], row["proposal_id"], row["rev"], row["path"], row["side"],
                  row["hunk"], cands)  # fmt: skip
