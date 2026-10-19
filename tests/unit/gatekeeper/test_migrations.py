"""Migrations (DESIGN §14.20.2): from an empty DB and from the previous number."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

from ossbot.gatekeeper import db

DECISION_TABLES = {"proposal_approvals", "skill_approvals", "tg_views", "tg_thread_seq"}
OUTBOX_COLUMNS = {"result_message_id", "bind_tokens_json", "view_json"}
CR004_TABLES = {"runner_requests"}
HERMES_TABLES = {"skill_reviews", "chat_turns", "chat_sessions"}  # 0004_hermes.sql (FEAT-007)
UX_TABLES = {"tg_topics", "pending_input", "review_comments", "ux_prefs"}  # 0005_telegram_ux.sql (FEAT-009)
UX_OUTBOX_COLUMNS = {"priority", "thread_key", "coalesce_key", "merged_into"}
LATEST = 5


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_from_empty(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "a.db")
    assert db.migrate(conn) == LATEST
    assert _tables(conn) >= DECISION_TABLES | CR004_TABLES | HERMES_TABLES | UX_TABLES
    assert _columns(conn, "tg_outbox") >= OUTBOX_COLUMNS | UX_OUTBOX_COLUMNS
    assert "comments_json" in _columns(conn, "change_requests")
    assert db.migrate(conn) == LATEST  # idempotent


def test_from_version_4_keeps_outbox_rows(tmp_path: Path) -> None:
    """0005 adds the outbox priority/thread/coalesce columns to existing rows (P2 by default)."""
    conn = db.connect(tmp_path / "d.db")
    files = resources.files("ossbot.gatekeeper.migrations")
    for number, name in ((1, "0001_init.sql"), (2, "0002_decisions.sql"), (3, "0003_cr004.sql"),
                         (4, "0004_hermes.sql")):  # fmt: skip
        sql = (files / name).read_text(encoding="utf-8")
        conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
    conn.execute(
        "INSERT INTO tg_outbox (method, payload_json, status, next_attempt_at) VALUES ('m', '{}', 'pending', 'x')"
    )
    conn.execute("INSERT INTO change_requests (id, cid, text, created_at) VALUES ('r', 'c', 't', 'x')")
    assert db.migrate(conn) == LATEST
    row = conn.execute("SELECT priority, thread_key, coalesce_key, merged_into FROM tg_outbox").fetchone()
    assert tuple(row) == (2, None, None, None)
    assert conn.execute("SELECT comments_json FROM change_requests").fetchone()[0] is None
    kinds = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'pending_input'").fetchone()[0]
    for kind in ("changes", "edit_comment", "proposal_adjust", "proposal_snooze_days", "feature_fix"):
        assert f"'{kind}'" in kinds


def test_from_version_1_keeps_rows(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "b.db")
    sql = (resources.files("ossbot.gatekeeper.migrations") / "0001_init.sql").read_text(encoding="utf-8")
    conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = 1;\nCOMMIT;")
    conn.execute(
        "INSERT INTO tg_outbox (method, payload_json, status, next_attempt_at) VALUES ('m', '{}', 'sent', 'x')"
    )
    assert db.migrate(conn) == LATEST
    row = conn.execute("SELECT method, result_message_id, bind_tokens_json, view_json FROM tg_outbox").fetchone()
    assert tuple(row) == ("m", None, None, None)


def test_from_version_2_rebuilds_contributions_and_keeps_references(tmp_path: Path) -> None:
    """0003 rebuilds contributions (source 'user'); rows and the foreign keys onto it survive."""
    conn = db.connect(tmp_path / "c.db")
    files = resources.files("ossbot.gatekeeper.migrations")
    for number, name in ((1, "0001_init.sql"), (2, "0002_decisions.sql")):
        sql = (files / name).read_text(encoding="utf-8")
        conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
    conn.execute(
        "INSERT INTO contributions (id, upstream, issue_number, source, state, created_at, updated_at)"
        " VALUES ('c_0000000001', 'o/r', 5, 'discover', 'VETTED', 't', 't')"
    )
    conn.execute(
        "INSERT INTO stage_runs (id, cid, stage, attempt, status) VALUES ('r1', 'c_0000000001', 'vet', 1, 'succeeded')"
    )
    assert db.migrate(conn) == LATEST
    assert _tables(conn) >= CR004_TABLES | {"proposals", "outcomes", "discovery_prefs", "discovery_prefs_undo"}
    assert {"work_url", "notes", "issue_hash", "proposal_version", "size_report_json"} <= _columns(
        conn, "contributions"
    )
    assert conn.execute("SELECT state FROM contributions").fetchone()[0] == "VETTED"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.execute(
        "INSERT INTO contributions (id, upstream, source, state, created_at, updated_at)"
        " VALUES ('c_0000000002', 'o/r', 'user', 'DISCOVERED', 't', 't')"
    )
    import sqlite3

    try:
        conn.execute(
            "INSERT INTO stage_runs (id, cid, stage, attempt, status) VALUES ('r2', 'c_nope', 'vet', 1, 'queued')"
        )
    except sqlite3.IntegrityError:
        pass
    else:  # pragma: no cover - the foreign key must still be enforced on the rebuilt table
        raise AssertionError("foreign key onto contributions not enforced after the rebuild")
