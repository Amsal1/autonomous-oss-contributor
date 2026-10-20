"""Audit chain, append-only triggers and anchors (DESIGN §12.7, AC 16)."""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
from pathlib import Path

import pytest

from ossbot.common.clock import FakeClock
from ossbot.gatekeeper import db
from ossbot.gatekeeper.anchors import AnchorClient, last_anchor_time, parse_anchors
from ossbot.gatekeeper.audit import GENESIS, AuditLog, row_hash, verify


@pytest.fixture
def audit_db(tmp_path: Path) -> tuple[sqlite3.Connection, AuditLog]:
    conn = db.connect(tmp_path / "a.db")
    db.migrate(conn)
    return conn, AuditLog(conn, FakeClock())


def append(conn: sqlite3.Connection, log: AuditLog, n: int) -> None:
    for i in range(n):
        with db.transaction(conn):
            log.append("system", "test_event", {"i": i, "text": "é"}, "c_0000000000")


def anchor_file(path: Path, conn: sqlite3.Connection, seqs: list[int]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for seq in seqs:
            r = conn.execute("SELECT seq, row_hash, ts FROM audit_events WHERE seq = ?", (seq,)).fetchone()
            fh.write(json.dumps({"seq": r["seq"], "row_hash": r["row_hash"], "ts": r["ts"]}) + "\n")


def test_chain_verifies(audit_db: tuple[sqlite3.Connection, AuditLog]) -> None:
    conn, log = audit_db
    append(conn, log, 5)
    result = verify(conn)
    assert result.ok and result.rows == 5 and result.head.seq == 5
    first = conn.execute("SELECT * FROM audit_events WHERE seq = 1").fetchone()
    assert first["prev_hash"] == GENESIS
    assert first["row_hash"] == row_hash(GENESIS, 1, first["ts"], "system", "test_event", "c_0000000000",
                                         {"i": 0, "text": "é"})  # fmt: skip


def test_append_requires_transaction(audit_db: tuple[sqlite3.Connection, AuditLog]) -> None:
    _, log = audit_db
    with pytest.raises(RuntimeError):
        log.append("system", "x")


@pytest.mark.parametrize("statement", ["UPDATE audit_events SET actor = 'evil'", "DELETE FROM audit_events"])
def test_triggers_block_update_and_delete(audit_db: tuple[sqlite3.Connection, AuditLog], statement: str) -> None:
    conn, log = audit_db
    append(conn, log, 2)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute(statement)


def test_edit_without_recompute_detected(audit_db: tuple[sqlite3.Connection, AuditLog]) -> None:
    conn, log = audit_db
    append(conn, log, 3)
    conn.executescript("DROP TRIGGER audit_no_update; UPDATE audit_events SET actor = 'evil' WHERE seq = 2;")
    result = verify(conn)
    assert not result.ok and any("seq 2" in e for e in result.errors)


def test_recomputed_chain_fails_against_anchors(audit_db: tuple[sqlite3.Connection, AuditLog], tmp_path: Path) -> None:
    """Drop triggers, edit a row, recompute every hash: the chain verifies alone, anchors disagree."""
    conn, log = audit_db
    append(conn, log, 6)
    anchors_path = tmp_path / "audit-anchors.log"
    anchor_file(anchors_path, conn, [3, 6])
    conn.executescript("DROP TRIGGER audit_no_update; DROP TRIGGER audit_no_delete;")
    conn.execute("UPDATE audit_events SET data_json = ? WHERE seq = 2", (json.dumps({"i": 999}),))
    prev = GENESIS
    for r in conn.execute("SELECT * FROM audit_events ORDER BY seq").fetchall():
        digest = row_hash(prev, r["seq"], r["ts"], r["actor"], r["event"], r["cid"], json.loads(r["data_json"]))
        conn.execute("UPDATE audit_events SET prev_hash = ?, row_hash = ? WHERE seq = ?", (prev, digest, r["seq"]))
        prev = digest
    assert verify(conn).ok  # the forged chain is internally consistent ...
    anchors, errors = parse_anchors(anchors_path)
    assert not errors and len(anchors) == 2
    result = verify(conn, anchors)
    assert not result.ok  # ... but every anchor at or after the edit disagrees
    assert sum("disagrees" in e for e in result.errors) == 2


def test_truncated_log_fails_against_anchors(audit_db: tuple[sqlite3.Connection, AuditLog], tmp_path: Path) -> None:
    conn, log = audit_db
    append(conn, log, 4)
    anchors_path = tmp_path / "audit-anchors.log"
    anchor_file(anchors_path, conn, [4])
    conn.executescript("DROP TRIGGER audit_no_delete; DELETE FROM audit_events WHERE seq = 4;")
    anchors, _ = parse_anchors(anchors_path)
    result = verify(conn, anchors)
    assert not result.ok and "missing" in result.errors[0]


def test_parse_anchors_reports_malformed(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    path.write_text('{"seq": 1, "row_hash": "x", "ts": "t"}\nnot json\n\n')
    anchors, errors = parse_anchors(path)
    assert len(anchors) == 1 and len(errors) == 1
    assert parse_anchors(tmp_path / "missing.log") == ([], [])


def test_last_anchor_time_reads_the_newest_valid_line(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    old = '{"seq": 1, "row_hash": "x", "ts": "2026-10-20T10:00:00Z"}\n' * 500
    path.write_text(old + '{"seq": 2, "row_hash": "y", "ts": "2026-10-20T10:05:00Z"}\nnot json\n')
    when = last_anchor_time(path)
    assert when is not None and when.isoformat() == "2026-10-20T10:05:00+00:00"
    assert last_anchor_time(tmp_path / "missing.log") is None
    (tmp_path / "empty.log").write_text("")
    assert last_anchor_time(tmp_path / "empty.log") is None


def test_anchor_client_trigger(tmp_path: Path) -> None:
    sock_path = tmp_path / "anchor.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)

    def serve() -> None:
        conn, _ = server.accept()
        with conn:
            conn.recv(1)
            conn.sendall(b"OK\n")

    thread = threading.Thread(target=serve)
    thread.start()
    assert AnchorClient(sock_path).trigger() is True
    thread.join(2)
    server.close()
    assert AnchorClient(tmp_path / "absent.sock").trigger() is False  # never raises
