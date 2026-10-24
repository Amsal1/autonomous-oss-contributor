"""Egress notes (build-env design §5.2, ADR-0025): the quiet half of the egress guard.

sandboxd's meter posts a note when a web job of ``sandbox_full`` went over a volume limit (``volume``: the job
ran on, nothing was held) or when a job saw floor notes (``floor_note``: a name or literal that resolves to
loopback, the HashiCorp checkpoint beacon). A note is data for the daily digest and ``ossctl egress notes``: no
state transition (I13), no approval invalidation, no Telegram message at write time, and never a go action (I9).

This module is the only SQL writer of ``egress_notes`` (import-linter contract; imported only by
``api/contributions.py`` for the POST, ``api/egress.py`` for the GET and ``telegram/digest.py``). Every value
comes from the squid log through sandboxd, is bounded by ``EgressNoteRequest`` and is only rendered escaped.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ossbot.common.clock import Clock, to_iso
from ossbot.common.schemas import EgressNoteRequest, EgressNoteView
from ossbot.gatekeeper import db
from ossbot.gatekeeper.audit import AuditLog

LIST_MAX = 200


class NoteRefused(RuntimeError):
    def __init__(self, status: int, code: str, detail: str = "") -> None:
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(f"{status} {code}{': ' + detail if detail else ''}")


def view(row: sqlite3.Row | dict[str, Any]) -> EgressNoteView:
    return EgressNoteView(
        id=int(row["id"]), cid=str(row["cid"]), run_id=str(row["run_id"]), stage=row["stage"], kind=row["kind"],
        summary=EgressNoteRequest.model_validate(json.loads(row["summary_json"])), created_at=str(row["created_at"]),
    )  # fmt: skip


class EgressNotes:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, audit: AuditLog) -> None:
        self.conn = conn
        self.clock = clock
        self.audit = audit

    def record(self, cid: str, req: EgressNoteRequest, actor: str) -> tuple[EgressNoteView, bool]:
        """(the note, created). One row per job run: a repeat of the same ``run_id`` returns the stored note
        (idempotent: sandboxd retries on transport errors)."""
        now = to_iso(self.clock.now())
        with db.transaction(self.conn):
            if db.one(self.conn, "SELECT 1 FROM contributions WHERE id = ?", (cid,)) is None:
                raise NoteRefused(404, "unknown_cid", cid)
            existing = db.one(self.conn, "SELECT * FROM egress_notes WHERE run_id = ?", (req.run_id,))
            if existing is not None:
                return view(existing), False
            summary = req.model_dump(mode="json")
            cur = self.conn.execute(
                "INSERT INTO egress_notes (cid, run_id, stage, kind, summary_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (cid, req.run_id, req.stage, req.kind, json.dumps(summary, sort_keys=True), now),
            )
            data = {"note": cur.lastrowid, "run": req.run_id, "kind": req.kind, "breached": req.breached}
            self.audit.append(actor, "egress_note_recorded", data, cid)
            row = db.one(self.conn, "SELECT * FROM egress_notes WHERE id = ?", (cur.lastrowid,))
            assert row is not None
        return view(row), True

    def since(
        self, since_iso: str | None = None, cid: str | None = None, limit: int = LIST_MAX
    ) -> list[EgressNoteView]:
        """Read-only: notes created at or after ``since_iso`` (all when None), newest first, optionally of one cid."""
        where, params = [], []
        if since_iso is not None:
            where.append("created_at >= ?")
            params.append(since_iso)
        if cid is not None:
            where.append("cid = ?")
            params.append(cid)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self.conn.execute(
            f"SELECT * FROM egress_notes{clause} ORDER BY created_at DESC, id DESC LIMIT ?",  # noqa: S608 - fixed parts
            (*params, max(1, min(limit, LIST_MAX))),
        )
        return [view(r) for r in rows]
