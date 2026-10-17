"""Append-only, hash-chained audit log (DESIGN §12.7, I11).

row_hash = sha256(prev_hash || canonical_json([seq, ts, actor, event, cid, data])) with the genesis
prev_hash = 64 zeros. Triggers block UPDATE/DELETE; they stop accidents, not the gatekeeper
itself, so ``verify`` also checks the chain against root-written anchors (anchors.py).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ossbot.common.clock import Clock, to_iso
from ossbot.common.hashing import canonical_json, sha256_hex

GENESIS = "0" * 64


def row_hash(prev_hash: str, seq: int, ts: str, actor: str, event: str, cid: str | None, data: Any) -> str:
    return sha256_hex(prev_hash.encode("ascii") + canonical_json([seq, ts, actor, event, cid, data]))


@dataclass(frozen=True)
class Anchor:
    seq: int
    row_hash: str
    ts: str


@dataclass(frozen=True)
class AuditHead:
    seq: int
    row_hash: str


@dataclass
class VerifyResult:
    ok: bool
    head: AuditHead
    rows: int
    anchors_checked: int = 0
    errors: list[str] = field(default_factory=list)


class AuditLog:
    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        self.conn = conn
        self.clock = clock

    def head(self) -> AuditHead:
        row = self.conn.execute("SELECT seq, row_hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
        if row is None:
            return AuditHead(0, GENESIS)
        return AuditHead(int(row["seq"]), str(row["row_hash"]))

    def append(self, actor: str, event: str, data: dict[str, Any] | None = None, cid: str | None = None) -> AuditHead:
        """Append one event. Must run inside the caller's transaction (db.transaction)."""
        if not self.conn.in_transaction:
            raise RuntimeError("audit.append must run inside a transaction")
        payload = data or {}
        head = self.head()
        seq = head.seq + 1
        ts = to_iso(self.clock.now())
        digest = row_hash(head.row_hash, seq, ts, actor, event, cid, payload)
        self.conn.execute(
            "INSERT INTO audit_events (seq, ts, actor, event, cid, data_json, prev_hash, row_hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (seq, ts, actor, event, cid, canonical_json(payload).decode("utf-8"), head.row_hash, digest),
        )
        return AuditHead(seq, digest)

    def count_since(self, event: str, since_iso: str, actor: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM audit_events WHERE event = ? AND ts >= ?"
        params: list[Any] = [event, since_iso]
        if actor is not None:
            sql += " AND actor = ?"
            params.append(actor)
        return int(self.conn.execute(sql, params).fetchone()[0])


def verify(conn: sqlite3.Connection, anchors: Sequence[Anchor] = ()) -> VerifyResult:
    """Recompute the chain and check every anchor's (seq, row_hash)."""
    errors: list[str] = []
    prev = GENESIS
    expected_seq = 1
    hashes: dict[int, str] = {}
    rows = 0
    for row in conn.execute("SELECT * FROM audit_events ORDER BY seq"):
        rows += 1
        seq = int(row["seq"])
        if seq != expected_seq:
            errors.append(f"seq gap: expected {expected_seq}, got {seq}")
        if row["prev_hash"] != prev:
            errors.append(f"seq {seq}: prev_hash does not link to the previous row")
        try:
            data = json.loads(row["data_json"])
        except ValueError:
            errors.append(f"seq {seq}: data_json is not JSON")
            data = None
        digest = row_hash(row["prev_hash"], seq, row["ts"], row["actor"], row["event"], row["cid"], data)
        if digest != row["row_hash"]:
            errors.append(f"seq {seq}: row_hash mismatch")
        hashes[seq] = str(row["row_hash"])
        prev = str(row["row_hash"])
        expected_seq = seq + 1
    head = AuditHead(expected_seq - 1, prev)
    for anchor in anchors:
        have = hashes.get(anchor.seq)
        if have is None:
            errors.append(f"anchor seq {anchor.seq} ({anchor.ts}) missing from the log")
        elif have != anchor.row_hash:
            errors.append(f"anchor seq {anchor.seq} ({anchor.ts}) disagrees with the log")
    return VerifyResult(ok=not errors, head=head, rows=rows, anchors_checked=len(anchors), errors=errors)
