"""Gatekeeper-side retention (DESIGN §18.10, ADR-0016 implementation addendum).

Who deletes what: the gatekeeper is the single writer of its DB and of ``/srv/ossbot/gatekeeper``
(0700), so it deletes its own data here, triggered only by the operator-only ``POST
/v1/retention/run`` (``api/retention.py`` is the only importer, import-linter). Root (``ossctl gc``)
deletes the root-reachable stores, among them ``/srv/ossbot/artifacts/<cid>`` for exactly the
``eligible_cids`` this module reports.

What goes, all time-based (``policy.retention``):

* closed ``tg_outbox`` / ``tg_views`` / ``ui_tokens`` rows older than ``telegram_rows_days``;
* the mirrors, derived dirs, imports and work bundles of terminal contributions untouched for
  ``contributions_days``, and the cache repos of upstreams no kept contribution uses.

Never touched: ``audit_events``, ``approvals``, ``proposal_approvals``, ``skill_approvals``,
``contributions``, ``outbound_actions`` (no statement here names them except in reads), anything of a
non-terminal contribution or one with an open action or approval, the current dashboard and an open
ForceReply prompt. Paths are never followed through a symlink and stay under ``gitops.root``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from ossbot.common.clock import to_iso
from ossbot.common.control_flags import read_flags
from ossbot.common.schemas import RETENTION_MAX_CIDS, TERMINAL_STATES, RetentionReport
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.gitops import GitOps, repo_key

log = logging.getLogger(__name__)

DIR_CATEGORIES = ("mirrors", "derived", "imports", "work", "cache")
ROW_TABLES = ("tg_outbox", "tg_views", "ui_tokens")
CHUNK = 500
_REV_GIT = re.compile(r"^(c_[0-9A-Z]{10})-r\d+\.git$")
_REV_BUNDLE = re.compile(r"^(c_[0-9A-Z]{10})-r\d+\.bundle$")
_CID = re.compile(r"^c_[0-9A-Z]{10}$")
_CACHE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*__[A-Za-z0-9._-]+\.git$")


@dataclass(frozen=True)
class Plan:
    """What one run selected; rows are already deleted (unless ``dry_run``) when ``prepare`` returns."""

    dry_run: bool
    snapshot_hash: str
    root: Path
    control_dir: Path
    eligible: list[str]
    rows: dict[str, int]
    paths: list[Path] = field(default_factory=list)

    def category(self, path: Path) -> str:
        return path.relative_to(self.root).parts[0]


# --------------------------------------------------------------------------- selection
def _marks(values: Sequence[object]) -> str:
    return ",".join("?" for _ in values)


def open_approval_ids(conn: sqlite3.Connection) -> set[str]:
    """Every id in approvals, proposal_approvals or skill_approvals that is pending, armed or confirmed."""
    sql = (
        "SELECT id FROM approvals WHERE status IN ('pending','armed','confirmed')"
        " UNION SELECT id FROM proposal_approvals WHERE status IN ('pending','armed','confirmed')"
        " UNION SELECT id FROM skill_approvals WHERE status IN ('pending','armed','confirmed')"
    )
    return {str(r[0]) for r in conn.execute(sql)}


_ELIGIBLE_SQL = (
    "SELECT c.id FROM contributions c WHERE c.state IN ({terminal}) AND c.updated_at < ?"
    " AND NOT EXISTS (SELECT 1 FROM outbound_actions a WHERE a.cid = c.id"
    " AND a.status IN ('pending_approval','approved','executing'))"
    " AND NOT EXISTS (SELECT 1 FROM approvals ap JOIN outbound_actions a ON a.id = ap.action_id"
    " WHERE a.cid = c.id AND ap.status IN ('pending','armed','confirmed'))"
    " AND NOT EXISTS (SELECT 1 FROM proposal_approvals p WHERE p.cid = c.id"
    " AND p.status IN ('pending','armed','confirmed'))"
    " ORDER BY c.updated_at, c.id LIMIT ?"
)


def eligible_contributions(conn: sqlite3.Connection, now: datetime, days: int) -> list[str]:
    """Terminal for ``days``, no open outbound action, no open approval for its actions or its cid."""
    terminal = sorted(s.value for s in TERMINAL_STATES)
    cutoff = to_iso(now - timedelta(days=days))
    sql = _ELIGIBLE_SQL.format(terminal=_marks(terminal))  # placeholders only
    return [str(r[0]) for r in conn.execute(sql, (*terminal, cutoff, RETENTION_MAX_CIDS))]


def select_outbox(conn: sqlite3.Connection, now: datetime, days: int, open_ids: set[str]) -> list[int]:
    cutoff = to_iso(now - timedelta(days=days))
    rows = conn.execute(
        "SELECT id, approval_id FROM tg_outbox WHERE status IN ('sent','failed','merged')"
        " AND next_attempt_at < ? ORDER BY id",
        (cutoff,),
    )
    return [int(r["id"]) for r in rows if r["approval_id"] is None or r["approval_id"] not in open_ids]


def select_views(
    conn: sqlite3.Connection, now: datetime, days: int, open_ids: set[str], eligible: Iterable[str]
) -> list[int]:
    cutoff = to_iso(now - timedelta(days=days))
    keep_cids = set(eligible)
    rows = conn.execute(
        "SELECT v.message_id, v.approval_id, v.cid FROM tg_views v WHERE v.updated_at < ?"
        " AND NOT (v.kind = 'dashboard' AND v.stale = 0)"
        " AND (v.action_id IS NULL OR EXISTS (SELECT 1 FROM outbound_actions a WHERE a.id = v.action_id"
        " AND a.status IN ('done','dry_run_done','failed','cancelled')))"
        " AND v.message_id NOT IN (SELECT prompt_message_id FROM pending_input"
        " WHERE consumed_at IS NULL AND expires_at > ?) ORDER BY v.message_id",
        (cutoff, to_iso(now)),
    )
    out: list[int] = []
    for r in rows:
        if r["approval_id"] is not None and r["approval_id"] in open_ids:
            continue
        if r["cid"] is None or r["cid"] in keep_cids:
            out.append(int(r["message_id"]))
    return out


def select_tokens(conn: sqlite3.Connection, now: datetime, days: int, open_ids: set[str]) -> list[str]:
    cutoff = to_iso(now - timedelta(days=days))
    rows = conn.execute(
        "SELECT token, approval_id FROM ui_tokens WHERE expires_at < ? OR used_at < ? ORDER BY token", (cutoff, cutoff)
    )
    return [str(r["token"]) for r in rows if r["approval_id"] is None or r["approval_id"] not in open_ids]


def _contained(root: Path, path: Path) -> bool:
    """``path`` exists, is not a symlink, and resolves strictly under ``root`` (no parent is a way out)."""
    try:
        st = os.lstat(path)
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
        return False
    real_root = root.resolve()
    return resolved != real_root and resolved.is_relative_to(real_root)


def _entries(directory: Path) -> list[Path]:
    """Children of a real directory (never of a symlink), sorted."""
    try:
        if not stat.S_ISDIR(os.lstat(directory).st_mode):
            return []
        return sorted(directory.iterdir())
    except OSError:
        return []


def select_dirs(ops: GitOps, conn: sqlite3.Connection, eligible: Sequence[str]) -> list[Path]:
    """Mirrors, derived dirs, imports and work bundles of eligible cids; cache repos no kept contribution uses."""
    root = ops.root
    wanted = set(eligible)
    out: list[Path] = []
    for key_dir in _entries(root / "mirrors"):
        for entry in _entries(key_dir):
            match = _REV_GIT.match(entry.name)
            if match and match.group(1) in wanted:
                out.append(entry)
    for name in ("derived", "imports"):
        out.extend(p for p in _entries(root / name) if _CID.match(p.name) and p.name in wanted)
    for entry in _entries(root / "work"):
        match = _REV_BUNDLE.match(entry.name)
        if match and match.group(1) in wanted:
            out.append(entry)
    protected: set[str] = set()
    for row in conn.execute("SELECT id, upstream FROM contributions"):
        if row["id"] in wanted:
            continue
        try:
            protected.add(f"{repo_key(str(row['upstream']))}.git")
        except ValueError:
            continue
    out.extend(p for p in _entries(root / "cache") if _CACHE.match(p.name) and p.name not in protected)
    return [p for p in out if _contained(root, p)]


# ------------------------------------------------------------------------------- run
def _delete(conn: sqlite3.Connection, table: str, key: str, ids: Sequence[object]) -> None:
    assert table in ROW_TABLES  # fixed names only; never audit or approval tables
    for start in range(0, len(ids), CHUNK):
        chunk = ids[start : start + CHUNK]
        conn.execute(f"DELETE FROM {table} WHERE {key} IN ({_marks(chunk)})", tuple(chunk))  # noqa: S608 - fixed names


def prepare(ctx: AppContext, dry_run: bool, actor: str) -> Plan:
    """Select everything, then in one transaction delete the rows (unless ``dry_run``) and append the
    ``retention_run`` audit event. Raises ``Blocked`` while paused or killed."""
    ctx.control.require_running("retention")
    policy = ctx.config.policy.retention
    now = ctx.clock.now()
    conn = ctx.conn
    with db.transaction(conn):
        open_ids = open_approval_ids(conn)
        eligible = eligible_contributions(conn, now, policy.contributions_days)
        outbox = select_outbox(conn, now, policy.telegram_rows_days, open_ids)
        views = select_views(conn, now, policy.telegram_rows_days, open_ids, eligible)
        tokens = select_tokens(conn, now, policy.telegram_rows_days, open_ids)
        paths = select_dirs(ctx.gitops, conn, eligible)
        plan = Plan(dry_run, ctx.snapshots.current.hash, ctx.gitops.root, ctx.paths.control_dir, eligible,
                    {"tg_outbox": len(outbox), "tg_views": len(views), "ui_tokens": len(tokens)}, paths)  # fmt: skip
        if not dry_run:
            _delete(conn, "tg_outbox", "id", outbox)
            _delete(conn, "tg_views", "message_id", views)
            _delete(conn, "ui_tokens", "token", tokens)
        ctx.audit.append(actor, "retention_run", {"dry_run": dry_run, "counts": counts(plan)})
    log.info("retention_run dry_run=%s counts=%s", dry_run, counts(plan))
    return plan


def counts(plan: Plan) -> dict[str, int]:
    out = {"contributions": len(plan.eligible), **plan.rows}
    for name in DIR_CATEGORIES:
        out[name] = 0
    for path in plan.paths:
        out[plan.category(path)] += 1
    return out


def _size(path: Path) -> int:
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size if stat.S_ISREG(st.st_mode) else 0
    total = 0
    for top, dirs, files in os.walk(path, followlinks=False):
        for name in (*dirs, *files):
            try:
                child = os.lstat(os.path.join(top, name))
            except OSError:
                continue
            if stat.S_ISREG(child.st_mode):
                total += child.st_size
    return total


def measure(plan: Plan) -> dict[str, int]:
    """Bytes per category (lstat, links never followed); filesystem only, safe off the event loop."""
    out = dict.fromkeys(DIR_CATEGORIES, 0)
    for path in plan.paths:
        out[plan.category(path)] += _size(path)
    return out


def remove(plan: Plan) -> int:
    """Delete the selected paths (never in a dry run); stops when a pause or kill appears. Returns failures."""
    if plan.dry_run:
        return 0
    failures = 0
    for path in plan.paths:
        if not read_flags(plan.control_dir).running:
            log.warning("retention_remove_stopped remaining=%d", len(plan.paths) - plan.paths.index(path))
            break
        if not _contained(plan.root, path):
            continue
        try:
            if stat.S_ISDIR(os.lstat(path).st_mode):
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            failures += 1
            log.warning("retention_remove_failed category=%s err=%s", plan.category(path), type(exc).__name__)
    return failures


def report(plan: Plan, sizes: dict[str, int]) -> RetentionReport:
    return RetentionReport(dry_run=plan.dry_run, snapshot_hash=plan.snapshot_hash, counts=counts(plan), bytes=sizes,
                           eligible_cids=list(plan.eligible))  # fmt: skip


def run(ctx: AppContext, dry_run: bool, actor: str = "system") -> RetentionReport:
    """Synchronous run (tests and callers without an event loop); the API splits it with ``to_thread``."""
    plan = prepare(ctx, dry_run, actor)
    sizes = measure(plan)
    remove(plan)
    return report(plan, sizes)
