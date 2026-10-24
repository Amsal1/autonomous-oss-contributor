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
from ossbot.common.config import OPTIONAL_MODES, EgressConfig
from ossbot.common.control_flags import (
    CODE_WEB_MODES,
    EGRESS_MODE_FILE,
    HERMES_TIER_FILE,
    Blocked,
    EgressMode,
    FlagState,
    HermesTier,
    KillLevel,
    LifecycleInfo,
    LifecycleScope,
    PauseInfo,
    lifecycle_name,
    read_egress_mode,
    read_flags,
    read_hermes_tier,
    read_lifecycle,
    write_flag,
)
from ossbot.common.paths import Paths
from ossbot.common.schemas import IMPLEMENT_NETWORK_VALUES, TERMINAL_STATES, ControlView, ImplementNetwork
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalService, Invalidated
from ossbot.gatekeeper.audit import AuditLog
from ossbot.gatekeeper.decisions import DecisionServices

log = logging.getLogger(__name__)
REQUEUED_SQL = (
    "SELECT COUNT(*) FROM contributions c JOIN stage_runs r ON r.cid = c.id"
    " WHERE r.rowid = (SELECT MAX(r2.rowid) FROM stage_runs r2 WHERE r2.cid = c.id)"
    " AND r.status IN ('interrupted', 'running')"
)
Notify = Callable[[str], None]
RESUME_LOG_MAX_LINE = 4096


class ControlConflict(RuntimeError):
    """409: e.g. resume while already running."""


class ModeChanged(RuntimeError):
    """control_checkpoint(): dry_run differs from the submission snapshot (R4-N2)."""


class EgressModeRefused(Exception):
    """An egress-mode switch refused before anything was written (build-env design §9.3): 409
    ``mode_not_in_snapshot``, 422 ``raise_implement_needs_sandbox_full``."""

    def __init__(self, status: int, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.status, self.code, self.detail = status, code, detail


class ImplementRaiseFailed(RuntimeError):
    """The egress mode was switched (flag, audit, FYI), but raising implement network to ``auto`` failed; the
    mode stays (design §9.2). The caller tells the human: "Egress mode switched; implement network unchanged"."""

    def __init__(self, previous_mode: EgressMode, implement_previous: ImplementNetwork, error: str) -> None:
        super().__init__(error)
        self.previous_mode, self.implement_previous, self.error = previous_mode, implement_previous, error


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

    def requeued(self) -> int:
        """Contributions the runner re-queues on up: a non-terminal item whose latest stage run was
        interrupted (drain timeout, down, crash) or is still marked running (the runner died; it
        marks those interrupted at its start). CR-001: the real "N resumed" of ``ossctl up``."""
        terminal = sorted(s.value for s in TERMINAL_STATES)
        marks = ",".join("?" * len(terminal))  # placeholders only; the values are bound
        sql = f"{REQUEUED_SQL} AND c.state NOT IN ({marks})"
        return int(self.conn.execute(sql, terminal).fetchone()[0])

    def up(self, actor: str, resumed: int | None = None) -> ControlView:
        """Back to scheduling work. Refused while killed (kill overrides lifecycle; use resume)."""
        flags = self.flags()
        if flags.killed is not None or self._row()["state"] == "killed":
            raise ControlConflict("killed: up refused; resume first (/resume, ossctl resume, resume.ps1)")
        if resumed is None:
            resumed = self.requeued()
        with db.transaction(self.conn):
            was = lifecycle_name(self.lifecycle())
            self.paths.lifecycle_flag.unlink(missing_ok=True)
            awaiting = int(
                self.conn.execute("SELECT COUNT(*) FROM approvals WHERE status IN ('pending','armed')").fetchone()[0]
            )
            self.audit.append(actor, "lifecycle_up", {"from": was, "resumed": resumed, "awaiting": awaiting})
        self.notify(f"▶️ Back up: {resumed} resumed, {awaiting} awaiting approval")
        return self.view()

    def request_lifecycle(
        self, action: Literal["down", "up"], actor: str, timeout_s: int, scope: LifecycleScope = "workers"
    ) -> dict[str, Any]:
        """Hand a workers-scope ``down`` or an ``up`` to the root helper (CR-001 5, PLAN item 42): write
        ``lifecycle-request`` (ossbot-lifecycle.path -> ossbot-lifecycle --request, which checks it is a
        regular file of the gatekeeper or root, at most 4 KiB and 5 min old, always consumes it, and
        runs ``ossctl down --scope workers`` or ``ossctl up``). Those report back through the usual
        drain/down/up endpoints (the "Going down" / "Back up" FYIs). ``up`` is refused while killed,
        here and again by root. Not a kill: approvals stay valid."""
        if scope != "workers":
            raise ControlConflict("only the workers scope is requested here; a full down is Windows-side (down.ps1)")
        if action == "up" and (self.flags().killed is not None or self._row()["state"] == "killed"):
            raise ControlConflict("killed: up refused; resume first (/resume, ossctl resume, resume.ps1)")
        request = {"action": action, "scope": scope, "actor": actor, "ts": to_iso(self.clock.now()),
                   "timeout_s": timeout_s}  # fmt: skip
        with db.transaction(self.conn):
            write_flag(self.paths.lifecycle_request, request)
            self.audit.append(actor, "lifecycle_requested", {"action": action, "scope": scope, "timeout_s": timeout_s})
        return {"requested": True, **{k: request[k] for k in ("action", "scope", "timeout_s")}}

    def lifecycle_status(self, *, egress_default: EgressMode, tier_default: HermesTier) -> dict[str, Any]:
        """One-screen state for ``ossctl status`` / ``status.ps1`` (CR-001 item 5, PLAN item 39): the
        lifecycle, running work, approvals, outbox, and the egress mode and Hermes tier in effect
        (flag, else the snapshot default; fail closed like every reader). The control view (kill,
        dry-run) and the Telegram poll stats are added by the endpoint, the last shutdown type and the
        Windows task states by ``ossctl status`` and ``status.ps1``."""
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
            "egress_mode": self.egress_mode(egress_default),
            "implement_network": self.implement_network(),
            "hermes_tier": self.hermes_tier(tier_default),
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

    def set_egress_mode(
        self, mode: EgressMode, actor: str, default: EgressMode, *, egress: EgressConfig, raise_implement: bool = False
    ) -> EgressMode:
        """Human-confirmed (ossctl TTY, Telegram /egress): write the flag, audit, FYI.

        Root applies it (ossbot-egress.path -> ossbot-egress-apply re-renders squid and restarts the
        proxy). Returns the previous mode. Only sandbox_open/sandbox_full change what fetch/validate reach
        (web_public; strict repos never), and the FYI says so. A switch invalidates nothing: approvals
        don't bind the egress mode, and holds, prefs and discovered targets are untouched (design §12.2).

        build-env design §9.2/§9.3: ``sandbox_full`` needs ``modes.sandbox_full`` in the snapshot (409
        ``mode_not_in_snapshot`` otherwise). ``raise_implement`` (only with ``sandbox_full``, else 422) then
        raises implement network to ``auto`` when it is lower, in its own transaction with its own audit, and one
        FYI names both. If that second step fails the mode stays (the main intent) and ``ImplementRaiseFailed``
        tells the caller. Leaving ``sandbox_full`` never changes implement network."""
        if raise_implement and mode != "sandbox_full":
            raise EgressModeRefused(422, "raise_implement_needs_sandbox_full")
        if mode in OPTIONAL_MODES and mode not in egress.modes:
            raise EgressModeRefused(409, "mode_not_in_snapshot", f"the config snapshot has no modes.{mode}")
        with db.transaction(self.conn):
            previous = self.egress_mode(default)
            write_flag(
                self.paths.control_dir / EGRESS_MODE_FILE,
                {"mode": mode, "actor": actor, "ts": to_iso(self.clock.now())},
            )
            self.audit.append(actor, "egress_mode_changed", {"from": previous, "to": mode})
        impl = self.implement_network()
        if raise_implement and IMPLEMENT_NETWORK_VALUES.index(impl) < IMPLEMENT_NETWORK_VALUES.index("auto"):
            try:
                self._write_implement_network("auto", actor, mode)
            except Exception as exc:
                log.error("egress mode %s set, but implement network could not be raised from %s: %s", mode, impl,
                          type(exc).__name__, extra={"event": "implement_network_raise_failed"})  # fmt: skip
                self._egress_fyi(previous, mode, actor, egress)
                raise ImplementRaiseFailed(previous, impl, f"{type(exc).__name__}: {exc}"[:300]) from exc
            self.notify(f"🌐 Egress mode {previous} → {mode} by {actor}; implement network {impl} → auto")
            return previous
        self._egress_fyi(previous, mode, actor, egress)
        return previous

    def _egress_fyi(self, previous: str, mode: str, actor: str, egress: EgressConfig) -> None:
        self.notify(
            f"🌐 Egress mode {previous} → {mode} by {actor} (fetch/validate {egress.repo_code_change(previous, mode)})"
        )

    # ------------------------------------------------- implement network (ADR-0024)
    def implement_network(self) -> ImplementNetwork:
        """The owner's setting for the coder's executor. A missing row or any unknown value is ``off``
        (fail closed); the column defaults to ``off`` on a fresh DB, after a reset and on upgrade."""
        try:
            row = db.read_control_row(self.conn)
        except sqlite3.Error:
            return "off"
        if row is None:
            return "off"
        try:
            value = row["implement_network"]
        except IndexError:  # a DB older than migration 0009
            return "off"
        return value if value in IMPLEMENT_NETWORK_VALUES else "off"

    def set_implement_network(self, value: ImplementNetwork, actor: str, default: EgressMode) -> ImplementNetwork:
        """Human-confirmed (Telegram /egress confirm, ossctl TTY confirm through the operator route): one
        transaction (the row, audit ``implement_network_changed``), then the FYI. Returns the previous value.
        A no-op set returns early without audit. Invalidates nothing (design §4c): approvals bind content,
        the config snapshot and dry-run, and this changes none of them; holds, prefs and the egress mode
        are untouched."""
        if value not in IMPLEMENT_NETWORK_VALUES:
            raise ValueError(f"implement_network: unknown value {value!r}")
        mode = self.egress_mode(default)
        previous = self._write_implement_network(value, actor, mode)
        if previous == value:
            return previous
        if value == "off":
            state = "the coder's executor never gets a network"
        else:
            state = "active" if mode in CODE_WEB_MODES else "inactive until egress mode sandbox_open"
        self.notify(f"🌐 Implement network {previous} → {value} by {actor} ({state})")
        return previous

    def _write_implement_network(self, value: ImplementNetwork, actor: str, mode: str) -> ImplementNetwork:
        """One transaction: the row and the audit ``implement_network_changed``; a no-op writes nothing.
        Returns the previous value (no FYI: the callers send their own)."""
        with db.transaction(self.conn):
            previous = self.implement_network()
            if previous == value:
                return previous
            self.conn.execute(
                "UPDATE control_state SET implement_network = ?, changed_by = ?, changed_at = ? WHERE id = 1",
                (value, actor, to_iso(self.clock.now())),
            )
            self.audit.append(actor, "implement_network_changed", {"from": previous, "to": value, "egress_mode": mode})
        return previous

    # ------------------------------------------------------ Hermes tier (§15.5, PLAN 23a)
    def hermes_tier(self, default: HermesTier) -> HermesTier:
        """The active tier: the flag, else ``hermes.capability_tier_default``; unparseable = locked."""
        return read_hermes_tier(self.paths.control_dir, default)

    def set_hermes_tier(
        self, tier: HermesTier, actor: str, default: HermesTier
    ) -> tuple[HermesTier, list[Invalidated]]:
        """Human-confirmed (ossctl TTY, Telegram /hermes tier later): write the flag, audit, FYI, and in
        the same transaction invalidate every open skill approval with ``tier_changed`` (delta 9).
        Applying the tier (config render, Hermes restart, probes) is PLAN item 36."""
        with db.transaction(self.conn):
            previous = self.hermes_tier(default)
            write_flag(
                self.paths.control_dir / HERMES_TIER_FILE,
                {"tier": tier, "actor": actor, "ts": to_iso(self.clock.now())},
            )
            invalidated = self.decisions.skills.invalidate_all_open("tier_changed", actor) if self.decisions else []
            self.audit.append(
                actor,
                "hermes_tier_changed",
                {"from": previous, "to": tier, "invalidated": [i.approval_id for i in invalidated]},
            )
        self.notify(f"🧠 Hermes tier {previous} → {tier} by {actor} ({len(invalidated)} skill card(s) invalidated)")
        return previous, invalidated

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
            entries = self._read_resume_log()
            for entry in entries:
                offline += 1
                self.audit.append("system", "operator_resume_offline", entry)
                fyis.append(
                    f"⚠️ Local operator action: RESUME (offline, via {entry.get('via', '?')}) "
                    f"by uid {entry.get('actor_uid', '?')} at {entry.get('ts', '?')}"
                )
            row = self._row()
            flags = self.flags()
            resumed_by_enforcer = (self._resume_pending or offline > 0) and flags.running
            removes_kill = any(entry.get("paused_only") is not True for entry in entries)
            if offline and flags.killed is None and removes_kill:
                # An offline resume (ossbot-resume) may have removed a root-written kill flag before
                # any reconcile saw it (P1-7): treat the resume.log line as evidence of that kill.
                # A ``paused_only`` line (ossctl backup lifting its own pause) never removes a kill flag,
                # so it is no such evidence (PLAN item 43).
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
