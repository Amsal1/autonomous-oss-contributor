"""Track record (DESIGN §9.14): proposal -> PR -> merge outcomes per repo, kind and size class.

Written from the state machine's transition (one transaction with the state change) and from
``/package`` (measured class). Only contributions that went through gate 1 have a row, so operator
imports do not skew the merge rate by size class the weekly digest shows (§14.13.4).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ossbot.common.schemas import State
from ossbot.gatekeeper import db

S = State
RESULTS: dict[State, str] = {
    S.PROPOSAL_REJECTED: "declined",
    S.ABANDONED: "abandoned",
    S.FAILED: "failed",
    S.EXPIRED: "expired",
    S.DRY_RUN_DONE: "dry_run",
    S.MERGED: "merged",
    S.CLOSED: "closed",
}
STAMPS: dict[State, str] = {S.PROPOSED: "proposed_at", S.PROPOSAL_APPROVED: "started_at", S.SUBMITTED: "pr_opened_at"}


def on_transition(conn: sqlite3.Connection, row: sqlite3.Row, to: State, now: str) -> None:
    """Called inside the transition's transaction with the contribution row read before the update."""
    if row["proposal_version"] is None and to is not S.PROPOSED:
        return
    if db.one(conn, "SELECT 1 FROM outcomes WHERE cid = ?", (row["id"],)) is None:
        conn.execute(
            "INSERT INTO outcomes (cid, upstream, kind, size_class, source, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (row["id"], row["upstream"], row["kind"], row["approved_class"] or row["est_class"], row["source"], now),
        )
    sets: dict[str, Any] = {"updated_at": now, "kind": row["kind"],
                            "size_class": row["approved_class"] or row["est_class"]}  # fmt: skip
    if to in STAMPS:
        column = STAMPS[to]
        current = db.one(conn, f"SELECT {column} FROM outcomes WHERE cid = ?", (row["id"],))  # noqa: S608
        if current is not None and current[0] is None:
            sets[column] = now
    if to in RESULTS:
        sets.update({"result": RESULTS[to], "finished_at": now})
    elif to in {S.PROPOSED, S.PROPOSAL_APPROVED}:  # Undo of a Skip clears the decline
        sets.update({"result": None, "finished_at": None})
    _update(conn, str(row["id"]), sets)


def set_measured(conn: sqlite3.Connection, cid: str, measured_class: str, now: str) -> None:
    row = db.one(conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
    if row is None or row["proposal_version"] is None:
        return
    if db.one(conn, "SELECT 1 FROM outcomes WHERE cid = ?", (cid,)) is None:
        conn.execute(
            "INSERT INTO outcomes (cid, upstream, kind, size_class, source, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (cid, row["upstream"], row["kind"], row["approved_class"] or row["est_class"], row["source"], now),
        )
    _update(conn, cid, {"measured_class": measured_class, "updated_at": now})


def _update(conn: sqlite3.Connection, cid: str, sets: dict[str, Any]) -> None:
    assignments = ", ".join(f"{k} = ?" for k in sets)
    conn.execute(f"UPDATE outcomes SET {assignments} WHERE cid = ?", (*sets.values(), cid))  # noqa: S608


def merge_rate_by_class(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """{class: {proposed, merged, closed, ...}} for the weekly digest (§14.13.4)."""
    out: dict[str, dict[str, int]] = {}
    for r in conn.execute("SELECT size_class, result, COUNT(*) AS n FROM outcomes GROUP BY size_class, result"):
        bucket = out.setdefault(str(r["size_class"] or "?"), {"proposed": 0})
        bucket["proposed"] += int(r["n"])
        if r["result"] is not None:
            bucket[str(r["result"])] = bucket.get(str(r["result"]), 0) + int(r["n"])
    return out
