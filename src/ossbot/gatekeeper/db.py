"""SQLite access: WAL, numbered migrations tracked by ``PRAGMA user_version`` (DESIGN §12.7).

Connections run in autocommit mode; every multi-statement change uses ``transaction()``
(``BEGIN IMMEDIATE``), so a state change and its audit event commit together.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any

MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    pass


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
    return conn


def _migrations() -> list[tuple[int, str, str]]:
    out = []
    for entry in resources.files("ossbot.gatekeeper.migrations").iterdir():
        match = MIGRATION_RE.match(entry.name)
        if match:
            out.append((int(match.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    out.sort()
    for index, (number, name, _) in enumerate(out, start=1):
        if number != index:
            raise MigrationError(f"migration numbering gap at {name}")
    return out


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations, each in its own transaction. Returns the new version."""
    current = schema_version(conn)
    migrations = _migrations()
    if current > len(migrations):
        raise MigrationError(f"database version {current} is newer than this code ({len(migrations)})")
    for number, name, sql in migrations:
        if number <= current:
            continue
        # SQLite's table-rebuild procedure (0003 rebuilds contributions): foreign keys off for the
        # migration, then foreign_key_check inside the transaction must come back empty.
        fk_on = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {number};")
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise sqlite3.IntegrityError(f"foreign_key_check: {[tuple(r) for r in broken[:5]]}")
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"{name}: {exc}") from exc
        finally:
            if fk_on:
                conn.execute("PRAGMA foreign_keys = ON")
    return schema_version(conn)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` ... ``COMMIT``; nested use joins the outer transaction."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(sql, params).fetchone()
    return row


def read_control_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return one(conn, "SELECT * FROM control_state WHERE id = 1")


def current_dry_run(conn: sqlite3.Connection) -> bool:
    row = read_control_row(conn)
    return True if row is None else bool(row["dry_run"])  # fail safe: no row = dry-run


def current_policy_hash(conn: sqlite3.Connection) -> str:
    row = read_control_row(conn)
    if row is None:
        raise RuntimeError("control_state not initialized")
    return str(row["config_hash"])
