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
from ossbot.common.control_flags import (
    EGRESS_MODE_FILE,
    Blocked,
    EgressMode,
    FlagState,
    KillLevel,
    LifecycleInfo,
    LifecycleScope,
    PauseInfo,
    lifecycle_name,
    read_egress_mode,
    read_flags,
    read_lifecycle,
    write_flag,
)
from ossbot.common.paths import Paths
from ossbot.common.schemas import ControlView
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalService, Invalidated
from ossbot.gatekeeper.audit import AuditLog
from ossbot.gatekeeper.decisions import DecisionServices

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
        *,
        decisions: DecisionServices | None = None,
    ) -> None:
        self.conn = conn
        self.paths = paths
        self.clock = clock
        self.audit = audit
        self.approvals = approvals
        self.decisions = decisions
        self.notify: Notify = notify or (lambda text: None)
        self._resume_pending = False

    def _kill_invalidate(self, actor: str) -> list[Invalidated]:
        """§12.4 "kill invalidates armed|confirmed", fanned out over all three decision services
        (delta 9); runs inside the caller's transaction."""
        if self.decisions is None:
            return self.approvals.invalidate_armed_confirmed("killed", actor)
        return self.decisions.invalidate_armed_confirmed("killed", actor)

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
            lifecycle=lifecycle_name(self.lifecycle()),
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
            invalidated = self._kill_invalidate(actor)
            self.audit.append(
                actor,
                "killed",
                {"reason": reason, "level": effective, "invalidated": [i.approval_id for i in invalidated]},
            )
        self.notify(f"🛑 Killed ({effective}) by {actor}: {reason}")
        return self.view()

    # ------------------------------------------------------------- lifecycle (CR-001)
    def lifecycle(self) -> LifecycleInfo | None:
        return read_lifecycle(self.paths.control_dir)

    def drain(self, actor: str, reason: str, timeout_s: int, scope: LifecycleScope = "distro") -> ControlView:
        """No new jobs, stages or submissions; running work may finish. Approvals stay valid."""
        with db.transaction(self.conn):
            current = self.lifecycle()
            if current is None or current.state != "draining" or current.scope != scope:
                write_flag(
                    self.paths.lifecycle_flag,
                    {"state": "draining", "scope": scope, "reason": reason, "actor": actor,
                     "ts": to_iso(self.clock.now()), "timeout_s": timeout_s},
                )  # fmt: skip
            self.audit.append(actor, "lifecycle_draining", {"reason": reason, "timeout_s": timeout_s, "scope": scope})
        self.notify(f"⏬ Draining ({actor}): no new work; running work gets up to {timeout_s} s")
        return self.view()

    def down(self, actor: str, finished: int, interrupted: int, scope: LifecycleScope = "distro") -> ControlView:
        with db.transaction(self.conn):
            current = self.lifecycle()
            write_flag(
                self.paths.lifecycle_flag,
                {"state": "down", "scope": scope, "reason": current.reason if current else "down", "actor": actor,
                 "ts": to_iso(self.clock.now()), "timeout_s": current.timeout_s if current else 0},
            )  # fmt: skip
            self.audit.append(
                actor, "lifecycle_down", {"finished": finished, "interrupted": interrupted, "scope": scope}
            )
        self.notify(f"⏹ Going down: {finished} finished, {interrupted} interrupted (re-queued on up)")
        return self.view()

    def up(self, actor: str, resumed: int = 0) -> ControlView:
        """Back to scheduling work. Refused while killed (kill overrides lifecycle; use resume)."""
        flags = self.flags()
        if flags.killed is not None or self._row()["state"] == "killed":
            raise ControlConflict("killed: up refused; resume first (/resume, ossctl resume, resume.ps1)")
        with db.transaction(self.conn):
            was = lifecycle_name(self.lifecycle())
            self.paths.lifecycle_flag.unlink(missing_ok=True)
            awaiting = int(
                self.conn.execute("SELECT COUNT(*) FROM approvals WHERE status IN ('pending','armed')").fetchone()[0]
            )
            self.audit.append(actor, "lifecycle_up", {"from": was, "resumed": resumed, "awaiting": awaiting})
        self.notify(f"▶️ Back up: {resumed} resumed, {awaiting} awaiting approval")
        return self.view()

    def lifecycle_status(self) -> dict[str, Any]:
        """One-screen state for ``ossctl status`` / ``status.ps1`` (CR-001 item 5)."""
        info = self.lifecycle()

        def count(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()[0])

        return {
            "lifecycle": lifecycle_name(info),
            "scope": info.scope if info else None,
            "lifecycle_since": info.ts if info else None,
            "running_stages": count("SELECT COUNT(*) FROM stage_runs WHERE status = 'running'"),
            "executing_actions": count("SELECT COUNT(*) FROM outbound_actions WHERE status = 'executing'"),
            "awaiting_approval": count("SELECT COUNT(*) FROM approvals WHERE status IN ('pending','armed')"),
            "outbox_pending": count("SELECT COUNT(*) FROM tg_outbox WHERE status = 'pending'"),
        }

    # ------------------------------------------------------------------ go (human only)
    def request_resume(self, actor: str, reason: str = "") -> None:
        """Human-confirmed resume: hand over to the root enforcer via ``resume-request``."""
        row = self._row()
        flags = self.flags()
        if row["state"] == "running" and flags.running:
            raise ControlConflict("already running")
        with db.transaction(self.conn):
            invalidated: list[Invalidated] = []
            if flags.killed is not None:
                # A root-written kill resumed before the next housekeeping tick never reaches
                # reconcile: apply §12.4 here, before the enforcer removes the flag (P1-7).
                invalidated = self._kill_invalidate(actor)
            write_flag(self.paths.resume_request, {"actor": actor, "reason": reason, "ts": to_iso(self.clock.now())})
            self.audit.append(
                actor, "resume_requested", {"reason": reason, "invalidated": [i.approval_id for i in invalidated]}
            )
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

    # ------------------------------------------------------------ egress mode (§11.5)
    def egress_mode(self, default: EgressMode) -> EgressMode:
        """The active mode: the flag, else the snapshot's ``mode_default``; unparseable = allowlist."""
        return read_egress_mode(self.paths.control_dir, default)

    def set_egress_mode(self, mode: EgressMode, actor: str, default: EgressMode) -> EgressMode:
        """Human-confirmed (ossctl TTY, Telegram /egress later): write the flag, audit, FYI.

        Root applies it (ossbot-egress.path -> ossbot-egress-apply re-renders squid and restarts the
        proxy). Returns the previous mode. The code-running profiles never change (§11.5)."""
        with db.transaction(self.conn):
            previous = self.egress_mode(default)
            write_flag(
                self.paths.control_dir / EGRESS_MODE_FILE,
                {"mode": mode, "actor": actor, "ts": to_iso(self.clock.now())},
            )
            self.audit.append(actor, "egress_mode_changed", {"from": previous, "to": mode})
        self.notify(f"🌐 Egress mode {previous} → {mode} by {actor} (fetch/validate unchanged)")
        return previous

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
            if offline and flags.killed is None:
                # An offline resume (ossbot-resume) may have removed a root-written kill flag before
                # any reconcile saw it (P1-7): treat the resume.log line as evidence of that kill.
                gone = self._kill_invalidate("resume_log")
                if gone:
                    self.audit.append(
                        "system",
                        "control_reconciled",
                        {"state": "resumed_offline", "invalidated": [i.approval_id for i in gone]},
                    )
                    fyis.append(f"🛑 Offline resume observed: {len(gone)} armed/confirmed approval(s) invalidated")
            if flags.killed is not None:
                # §12.4 "kill invalidates armed|confirmed": a flag written by root (watchdog,
                # ossbot-kill/kill.ps1, boot gate) bypasses kill(), so invalidate here, in the same
                # transaction. Repeated reconciles normally find nothing: the Telegram handler refuses
                # Approve while killed (ApprovalService.approve itself has no running check, so an
                # approval armed through another path is invalidated on the next tick).
                invalidated = self._kill_invalidate("flag")
                if row["state"] != "killed":
                    self._set("killed", flags.killed.reason, "flag")
                if row["state"] != "killed" or invalidated:
                    self.audit.append(
                        "system",
                        "control_reconciled",
                        {
                            "state": "killed",
                            "from": row["state"],
                            "level": flags.killed.level,
                            "invalidated": [i.approval_id for i in invalidated],
                        },
                    )
                if invalidated:
                    fyis.append(
                        f"🛑 Kill flag ({flags.killed.level}: {flags.killed.reason}) observed: "
                        f"{len(invalidated)} armed/confirmed approval(s) invalidated"
                    )
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
