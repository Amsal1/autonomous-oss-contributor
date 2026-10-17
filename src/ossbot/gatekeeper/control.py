"""Global control state: running / paused / killed (soft|full) and dry-run (DESIGN §13, I4, I7, I9).

* The flag files in the control dir are authoritative; ``control_state`` mirrors them and never
  disagrees towards "running" (N12).
* Dry-run defaults ON on a fresh DB (I7). Any toggle invalidates every open approval in the same
  transaction.
* Resume is human-only: the gatekeeper writes ``resume-request``; the root enforcer removes the
  flags (13.2). The offline path (``ossbot-resume``) appends to ``resume.log``; new lines become an
  ``operator_resume_offline`` audit event and an FYI on the next start (N4).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from ossbot.common.clock import Clock, to_iso
from ossbot.common.control_flags import Blocked, FlagState, KillLevel, PauseInfo, read_flags, write_flag
from ossbot.common.paths import Paths
from ossbot.common.schemas import ControlView
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalService, Invalidated
from ossbot.gatekeeper.audit import AuditLog

log = logging.getLogger(__name__)
Notify = Callable[[str], None]
RESUME_LOG_MAX_LINE = 4096


class ControlConflict(RuntimeError):
    """409: e.g. resume while already running."""


class ModeChanged(RuntimeError):
    """control_checkpoint(): dry_run differs from the submission snapshot (R4-N2)."""


@dataclass(frozen=True)
class ReconcileReport:
    state: Literal["running", "paused", "killed"]
    alerts: tuple[str, ...]
    offline_resumes: int


class ControlService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        paths: Paths,
        clock: Clock,
        audit: AuditLog,
        approvals: ApprovalService,
        notify: Notify | None = None,
    ) -> None:
        self.conn = conn
        self.paths = paths
        self.clock = clock
        self.audit = audit
        self.approvals = approvals
        self.notify: Notify = notify or (lambda text: None)
        self._resume_pending = False

    # ----------------------------------------------------------------- basics
    def flags(self) -> FlagState:
        return read_flags(self.paths.control_dir)

    def _row(self) -> sqlite3.Row:
        row = db.read_control_row(self.conn)
        if row is None:
            raise RuntimeError("control_state not initialized")
        return row

    def _set(self, state: str, reason: str | None, actor: str) -> None:
        self.conn.execute(
            "UPDATE control_state SET state = ?, reason = ?, changed_by = ?, changed_at = ? WHERE id = 1",
            (state, reason, actor, to_iso(self.clock.now())),
        )

    def initialize(self, config_hash: str) -> bool:
        """Create the control row on a fresh DB: running, dry-run ON (I7). True if created."""
        with db.transaction(self.conn):
            if db.read_control_row(self.conn) is not None:
                return False
            self.conn.execute(
                "INSERT INTO control_state (id, state, dry_run, config_hash, reason, changed_by, changed_at)"
                " VALUES (1, 'running', 1, ?, 'initialized', 'system', ?)",
                (config_hash, to_iso(self.clock.now())),
            )
            self.audit.append("system", "control_initialized", {"dry_run": True, "config_hash": config_hash})
        return True

    def view(self) -> ControlView:
        row = self._row()
        flags = self.flags()
        state = self._effective_state(row, flags)
        return ControlView(
            state=state,
            dry_run=bool(row["dry_run"]),
            reason=(flags.killed.reason if flags.killed else flags.paused.reason if flags.paused else row["reason"]),
            since=row["changed_at"],
            config_hash=row["config_hash"],
            audit_head=self.audit.head().row_hash,
            kill_level=flags.killed.level if flags.killed else None,
        )

    @staticmethod
    def _effective_state(row: sqlite3.Row, flags: FlagState) -> Literal["running", "paused", "killed"]:
        if flags.killed is not None or row["state"] == "killed":
            return "killed"
        if flags.paused is not None or row["state"] == "paused":
            return "paused"
        return "running"

    # ------------------------------------------------------------------ stop (any role)
    def pause(self, reason: str, actor: str) -> ControlView:
        with db.transaction(self.conn):
            flags = self.flags()
            if flags.paused is None:
                write_flag(self.paths.paused_flag, {"reason": reason, "actor": actor, "ts": to_iso(self.clock.now())})
            if flags.killed is None and self._row()["state"] != "killed":
                self._set("paused", reason, actor)
            self.audit.append(actor, "paused", {"reason": reason})
        self.notify(f"⏸ Paused by {actor}: {reason}")
        return self.view()

    def kill(self, reason: str, actor: str, level: KillLevel = "soft") -> ControlView:
        with db.transaction(self.conn):
            existing = self.flags().killed
            effective: KillLevel = "full" if level == "full" or (existing and existing.level == "full") else "soft"
            if existing is None or existing.level != effective:
                write_flag(
                    self.paths.killed_flag,
                    {"level": effective, "reason": reason, "actor": actor, "ts": to_iso(self.clock.now())},
                )
            self._set("killed", reason, actor)
            invalidated = self.approvals.invalidate_armed_confirmed("killed", actor)
            self.audit.append(
                actor,
                "killed",
                {"reason": reason, "level": effective, "invalidated": [i.approval_id for i in invalidated]},
            )
        self.notify(f"🛑 Killed ({effective}) by {actor}: {reason}")
        return self.view()

    # ------------------------------------------------------------------ go (human only)
    def request_resume(self, actor: str, reason: str = "") -> None:
        """Human-confirmed resume: hand over to the root enforcer via ``resume-request``."""
        row = self._row()
        flags = self.flags()
        if row["state"] == "running" and flags.running:
            raise ControlConflict("already running")
        with db.transaction(self.conn):
            write_flag(self.paths.resume_request, {"actor": actor, "reason": reason, "ts": to_iso(self.clock.now())})
            self.audit.append(actor, "resume_requested", {"reason": reason})
        self._resume_pending = True

    def set_dry_run(self, enabled: bool, actor: str) -> list[Invalidated]:
        with db.transaction(self.conn):
            row = self._row()
            if bool(row["dry_run"]) == enabled:
                return []
            self.conn.execute(
                "UPDATE control_state SET dry_run = ?, changed_by = ?, changed_at = ? WHERE id = 1",
                (int(enabled), actor, to_iso(self.clock.now())),
            )
            invalidated = self.approvals.invalidate_all_open("dry_run_toggled", actor)
            self.audit.append(actor, "dry_run_set", {"enabled": enabled, "invalidated": len(invalidated)})
        return invalidated

    def set_config_hash(self, config_hash: str) -> None:
        """Called inside the snapshot manager's transaction."""
        self.conn.execute("UPDATE control_state SET config_hash = ? WHERE id = 1", (config_hash,))

    # ---------------------------------------------------------------- checks
    def require_running(self, what: str) -> None:
        flags = self.flags()
        row = self._row()
        if not flags.running:
            raise Blocked(what, flags)
        if row["state"] != "running":
            raise Blocked(what, FlagState(killed=None, paused=PauseInfo(str(row["reason"]), "db", row["changed_at"])))

    def checkpoint(self, what: str, snapshot_dry_run: bool) -> None:
        """``control_checkpoint()`` before every individual outbound write (M5, R4-N2)."""
        self.require_running(what)
        if bool(self._row()["dry_run"]) != snapshot_dry_run:
            raise ModeChanged(f"{what}: dry-run changed during submission")

    # ---------------------------------------------------------------- reconcile
    def _read_resume_log(self) -> list[dict[str, Any]]:
        offset = int(self._row()["resume_log_offset"])
        try:
            with self.paths.resume_log.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                if size < offset:  # rotated or truncated: start again from the beginning
                    offset = 0
                fh.seek(offset)
                data = fh.read()
        except FileNotFoundError:
            return []
        entries: list[dict[str, Any]] = []
        consumed = 0
        for raw in data.split(b"\n")[:-1]:  # the last element is an unterminated partial line
            consumed += len(raw) + 1
            try:
                doc = json.loads(raw[:RESUME_LOG_MAX_LINE].decode("utf-8"))
                entries.append(doc if isinstance(doc, dict) else {"raw": str(doc)})
            except (ValueError, UnicodeDecodeError):
                entries.append({"raw": raw[:200].decode("utf-8", "replace")})
        if consumed:
            self.conn.execute("UPDATE control_state SET resume_log_offset = ? WHERE id = 1", (offset + consumed,))
        return entries

    def reconcile(self) -> ReconcileReport:
        """Bring control_state in line with the flag files (N12) and announce new resume.log lines (N4).

        Runs at start and periodically (a soft-killed gatekeeper keeps running across an offline resume).
        """
        alerts: list[str] = []
        fyis: list[str] = []
        offline = 0
        with db.transaction(self.conn):
            for entry in self._read_resume_log():
                offline += 1
                self.audit.append("system", "operator_resume_offline", entry)
                fyis.append(
                    f"⚠️ Local operator action: RESUME (offline, via {entry.get('via', '?')}) "
                    f"by uid {entry.get('actor_uid', '?')} at {entry.get('ts', '?')}"
                )
            row = self._row()
            flags = self.flags()
            resumed_by_enforcer = (self._resume_pending or offline > 0) and flags.running
            if flags.killed is not None:
                if row["state"] != "killed":
                    self._set("killed", flags.killed.reason, "flag")
                    self.audit.append("system", "control_reconciled", {"state": "killed", "from": row["state"]})
            elif flags.paused is not None:
                if row["state"] != "paused" and row["state"] != "killed":
                    self._set("paused", flags.paused.reason, "flag")
                    self.audit.append("system", "control_reconciled", {"state": "paused", "from": row["state"]})
                elif row["state"] == "killed":
                    alerts.append("kill flag missing; use /resume")
            elif row["state"] != "running":
                if resumed_by_enforcer and not self.paths.resume_request.exists():
                    self._set("running", "resumed", "enforcer")
                    self.audit.append("system", "resumed", {"from": row["state"], "offline": offline})
                    self._resume_pending = False
                elif row["state"] == "killed":
                    alerts.append("kill flag missing; use /resume")
                else:
                    write_flag(
                        self.paths.paused_flag,
                        {
                            "reason": str(row["reason"]),
                            "actor": "gatekeeper_reconcile",
                            "ts": to_iso(self.clock.now()),
                        },
                    )
                    alerts.append("pause flag missing; use /resume")
            for alert in alerts:
                self.audit.append("system", "control_alert", {"alert": alert})
            final = self._effective_state(self._row(), self.flags())
        for text in [*fyis, *(f"🚨 {a}" for a in alerts)]:
            self.notify(text)
        return ReconcileReport(state=final, alerts=tuple(alerts), offline_resumes=offline)
