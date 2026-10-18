"""Migrations (DESIGN §14.20.2): from an empty DB and from the previous number."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

from ossbot.gatekeeper import db

DECISION_TABLES = {"proposal_approvals", "skill_approvals", "tg_views", "tg_thread_seq"}
OUTBOX_COLUMNS = {"result_message_id", "bind_tokens_json", "view_json"}
CR004_TABLES = {"runner_requests"}
LATEST = 3


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_from_empty(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "a.db")
    assert db.migrate(conn) == LATEST
    assert _tables(conn) >= DECISION_TABLES | CR004_TABLES
    assert _columns(conn, "tg_outbox") >= OUTBOX_COLUMNS
    assert db.migrate(conn) == LATEST  # idempotent


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
