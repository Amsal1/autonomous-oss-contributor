"""sandboxd's ``jobs.exec_network`` column (ADR-0024, carried criterion C-D18): an idempotent add for a DB
created before it, present on a fresh DB."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ossbot.sandboxd.db import Store

OLD_JOBS = """
CREATE TABLE jobs (
  run_id TEXT PRIMARY KEY, cid TEXT NOT NULL, profile TEXT NOT NULL, entrypoint TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('queued','running','succeeded','failed','cancelled')),
  exit_code INTEGER, error_class TEXT, artifact_dir TEXT,
  created_at TEXT NOT NULL, finished_at TEXT);
INSERT INTO jobs (run_id, cid, profile, entrypoint, state, created_at)
  VALUES ('r1', 'c_01ABCDEFGH', 'fetch', 'fetch', 'succeeded', 't');
"""


def _columns(store: Store) -> list[str]:
    return [str(r["name"]) for r in store.conn.execute("PRAGMA table_info(jobs)")]


def test_old_schema_gets_the_column_once(tmp_path: Path) -> None:
    path = tmp_path / "sandboxd.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_JOBS)
    conn.close()
    for _ in range(2):
        store = Store(path)
        assert _columns(store).count("exec_network") == 1
        row = store.conn.execute("SELECT run_id, exec_network FROM jobs").fetchone()
        assert tuple(row) == ("r1", None)
        store.close()


def test_fresh_db_has_the_column(tmp_path: Path) -> None:
    store = Store(tmp_path / "fresh.db")
    assert "exec_network" in _columns(store)
    store.close()
