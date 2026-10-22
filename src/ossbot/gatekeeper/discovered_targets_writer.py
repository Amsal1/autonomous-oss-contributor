"""Every SQL write to ``discovered_targets`` (post-v1 open discovery, design §8.2).

Importable only from ``ossbot.gatekeeper.discovered_targets`` (import-linter contract), which owns every
lifecycle rule (§8.3); nothing here decides whether to write. Each function runs on the caller's open
connection inside the caller's ``db.transaction``. Column names are a closed set (never caller text),
so the statements stay static apart from the whitelisted names.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

ROW_COLUMNS: tuple[str, ...] = (
    "repo", "source", "status", "topic", "stars", "language", "license_spdx", "pushed_at",
    "default_branch_commit_at", "ci_on_pr", "repo_score", "skip_reason", "suggested_issue", "egress_strict",
    "first_seen", "updated_at", "tried_at", "checked_at", "cooldown_until", "decided_by", "decided_at",
)  # fmt: skip
FACT_COLUMNS: tuple[str, ...] = (
    "stars", "language", "license_spdx", "pushed_at", "default_branch_commit_at", "ci_on_pr", "repo_score",
)  # fmt: skip
STATUS_COLUMNS: tuple[str, ...] = (
    "skip_reason", "suggested_issue", "cooldown_until", "checked_at", "tried_at", "decided_by", "decided_at",
)  # fmt: skip


class WriterError(ValueError):
    """A column outside the closed set, or a write that matched no row."""


def _only(cols: Mapping[str, Any], allowed: tuple[str, ...]) -> None:
    bad = sorted(set(cols) - set(allowed))
    if bad:
        raise WriterError(f"columns not writable here: {bad}")


def _one_row(cur: sqlite3.Cursor, repo: str) -> None:
    if cur.rowcount != 1:
        raise WriterError(f"no discovered_targets row for {repo}")


def insert_row(conn: sqlite3.Connection, row: Mapping[str, Any]) -> None:
    """A new row; ``repo``, ``source``, ``status``, ``first_seen`` and ``updated_at`` are required."""
    _only(row, ROW_COLUMNS)
    names = list(row)
    conn.execute(
        f"INSERT INTO discovered_targets ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
        tuple(row[n] for n in names),
    )


def update_facts(conn: sqlite3.Connection, repo: str, facts: Mapping[str, Any], now: str, *,
                 topic: str | None = None) -> None:  # fmt: skip
    """Refresh the GitHub facts; ``topic`` only fills a NULL column (never overwrites)."""
    _only(facts, FACT_COLUMNS)
    sets = [f"{n} = ?" for n in facts] + ["topic = COALESCE(topic, ?)", "updated_at = ?"]
    cur = conn.execute(
        f"UPDATE discovered_targets SET {', '.join(sets)} WHERE repo = ?",
        (*facts.values(), topic, now, repo),
    )
    _one_row(cur, repo)


def set_status(conn: sqlite3.Connection, repo: str, status: str, now: str, **cols: Any) -> None:
    """Move a row to ``status``; ``cols`` from ``STATUS_COLUMNS`` are set in the same statement."""
    _only(cols, STATUS_COLUMNS)
    sets = ["status = ?", *(f"{n} = ?" for n in cols), "updated_at = ?"]
    cur = conn.execute(
        f"UPDATE discovered_targets SET {', '.join(sets)} WHERE repo = ?",
        (status, *cols.values(), now, repo),
    )
    _one_row(cur, repo)


def set_tried(conn: sqlite3.Connection, repo: str, tried_at: str, cooldown_until: str) -> None:
    """A candidate's issues were scanned this cycle."""
    cur = conn.execute(
        "UPDATE discovered_targets SET tried_at = ?, cooldown_until = ?, updated_at = ? WHERE repo = ?",
        (tried_at, cooldown_until, tried_at, repo),
    )
    _one_row(cur, repo)


def clear_tried(conn: sqlite3.Connection, repo: str, now: str) -> None:
    """The owner's ``ossctl targets discovered --untry`` (discovery pacing, 2026-10-22): the candidate is
    untried again, without a cooldown."""
    cur = conn.execute(
        "UPDATE discovered_targets SET tried_at = NULL, cooldown_until = NULL, updated_at = ? WHERE repo = ?",
        (now, repo),
    )
    _one_row(cur, repo)


def set_checked(conn: sqlite3.Connection, repo: str, checked_at: str, skip_reason: str | None) -> None:
    """A kept repo's re-check (§6.2): ``skip_reason`` set on a failure, cleared on a pass."""
    cur = conn.execute(
        "UPDATE discovered_targets SET checked_at = ?, skip_reason = ?, updated_at = ? WHERE repo = ?",
        (checked_at, skip_reason, checked_at, repo),
    )
    _one_row(cur, repo)


def set_strict(conn: sqlite3.Connection, repo: str, strict: bool, now: str, decided_by: str) -> None:
    """The per-repo ``egress: strict`` override (§12.3)."""
    cur = conn.execute(
        "UPDATE discovered_targets SET egress_strict = ?, decided_by = ?, decided_at = ?, updated_at = ?"
        " WHERE repo = ?",
        (1 if strict else 0, decided_by, now, now, repo),
    )
    _one_row(cur, repo)
