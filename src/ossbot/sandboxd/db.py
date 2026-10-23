"""sandboxd SQLite state (``/srv/ossbot/sandboxd/sandboxd.db``): subnets, jobs, safety probes, pending
egress-anomaly holds."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB = Path("/srv/ossbot/sandboxd/sandboxd.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS subnets (
  profile TEXT NOT NULL, idx INTEGER NOT NULL, cid TEXT NOT NULL, allocated_at TEXT NOT NULL,
  pool TEXT NOT NULL,
  PRIMARY KEY (pool, idx), UNIQUE (profile, cid));
CREATE TABLE IF NOT EXISTS jobs (
  run_id TEXT PRIMARY KEY, cid TEXT NOT NULL, profile TEXT NOT NULL, entrypoint TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('queued','running','succeeded','failed','cancelled')),
  exit_code INTEGER, error_class TEXT, artifact_dir TEXT,
  created_at TEXT NOT NULL, finished_at TEXT,
  exec_network TEXT);  -- ADR-0024: coder runs only, 'web' | 'none' (agent-exec's admission result)
-- open-discovery design §13.2 (review-3 M3): an egress anomaly whose gatekeeper hold isn't recorded.
-- While a row exists the cid's fetch/validate jobs are refused (423 egress_anomaly_pending). Rows without
-- rejected_code are retried every 30 s; a 4xx answer sets rejected_code and only ossctl egress release
-- clears the row.
CREATE TABLE IF NOT EXISTS anomaly_pending (
  cid TEXT PRIMARY KEY, run_id TEXT NOT NULL, summary_json TEXT NOT NULL, created_at TEXT NOT NULL,
  rejected_code TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_attempt_at TEXT);
CREATE TABLE IF NOT EXISTS safety_probes (
  kiro_cli_version TEXT NOT NULL, sandbox_llm_digest TEXT NOT NULL, sandbox_full_digest TEXT NOT NULL,
  agent_configs_sha256 TEXT NOT NULL,
  settings_sha256 TEXT NOT NULL, trust_mode TEXT NOT NULL CHECK (trust_mode IN ('ids','all')),
  probe TEXT NOT NULL, passed INTEGER NOT NULL, evidence_sha256 TEXT NOT NULL, ran_at TEXT NOT NULL,
  PRIMARY KEY (kiro_cli_version, sandbox_llm_digest, sandbox_full_digest, agent_configs_sha256, settings_sha256,
               trust_mode, probe));
"""


class Store:
    """One connection shared by the API thread pool; every access holds ``lock``."""

    def __init__(self, path: Path) -> None:
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        # ADR-0024 (carried criterion D18): a DB created before exec_network gets the column once
        columns = {str(r["name"]) for r in self.conn.execute("PRAGMA table_info(jobs)")}
        if "exec_network" not in columns:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN exec_network TEXT")
        self.lock = threading.RLock()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def close(self) -> None:
        self.conn.close()
