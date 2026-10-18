"""Stage runs (DESIGN §9.3, §12.7 ``stage_runs``): the runner's stage bookkeeping, owned by the gatekeeper.

The runner (worker) opens a run before each stage, heartbeats it every 15 s and closes it. Opening
a run is refused while paused or killed (423) and while draining or down (423 with the lifecycle
state), so a buggy runner can't start a stage past the control state (CR-001, AC 10). Interrupted
runs are re-queued by the runner from the last completed stage, at most 3 times per stage.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ossbot.common.clock import Clock, to_iso
from ossbot.common.control_flags import Blocked
from ossbot.common.ids import new_id
from ossbot.common.schemas import STAGE_ENTRYPOINTS, StageRunOpen, StageRunUpdate
from ossbot.gatekeeper import db
from ossbot.gatekeeper.audit import AuditLog
from ossbot.gatekeeper.control import ControlService

FINAL = frozenset({"succeeded", "failed", "interrupted", "cancelled"})
REQUEUE_MAX = 3  # interrupted re-queues per stage (§9.3)
# unbundle is a stage_runs row the FETCHED -> IMPLEMENTED guard reads (§9.13 step 5)
STAGE_NAMES: frozenset[str] = frozenset({*STAGE_ENTRYPOINTS, "unbundle"})


class StageRunRefused(RuntimeError):
    def __init__(self, status: int, code: str, detail: str = "") -> None:
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(f"{status} {code}{': ' + detail if detail else ''}")


class StageRuns:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, audit: AuditLog, control: ControlService) -> None:
        self.conn = conn
        self.clock = clock
        self.audit = audit
        self.control = control

    def open(self, cid: str, req: StageRunOpen, actor: str) -> dict[str, Any]:
        if req.stage not in STAGE_NAMES:
            raise StageRunRefused(400, "unknown_stage", req.stage)
        try:
            self.control.require_running(f"stage:{req.stage}")
        except Blocked as exc:
            raise StageRunRefused(423, exc.flags.state, str(exc)) from exc
        info = self.control.lifecycle()
        if info is not None:
            raise StageRunRefused(423, info.state, f"stage:{req.stage} while {info.state}")
        now = to_iso(self.clock.now())
        with db.transaction(self.conn):
            if db.one(self.conn, "SELECT 1 FROM contributions WHERE id = ?", (cid,)) is None:
                raise StageRunRefused(404, "not_found", cid)
            running = db.one(
                self.conn, "SELECT id FROM stage_runs WHERE cid = ? AND status = 'running' LIMIT 1", (cid,)
            )  # fmt: skip
            if running is not None:
                raise StageRunRefused(409, "stage_running", str(running["id"]))
            last_ok = db.one(
                self.conn,
                "SELECT MAX(attempt) FROM stage_runs WHERE cid = ? AND stage = ? AND status = 'succeeded'",
                (cid, req.stage),
            )
            attempt = int(
                self.conn.execute(
                    "SELECT COALESCE(MAX(attempt), 0) FROM stage_runs WHERE cid = ? AND stage = ?", (cid, req.stage)
                ).fetchone()[0]
            ) + 1  # fmt: skip
            interrupted = int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM stage_runs WHERE cid = ? AND stage = ? AND status = 'interrupted'"
                    " AND attempt > ?",
                    (cid, req.stage, int(last_ok[0] or 0) if last_ok else 0),
                ).fetchone()[0]
            )
            if interrupted >= REQUEUE_MAX:
                raise StageRunRefused(409, "requeue_limit", f"{req.stage} interrupted {interrupted} times")
            run_id = new_id("r")
            self.conn.execute(
                "INSERT INTO stage_runs (id, cid, stage, attempt, status, profile, coder_mode, model, effort,"
                " started_at, heartbeat_at) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)",
                (run_id, cid, req.stage, attempt, req.profile, req.coder_mode, req.model, req.effort, now, now),
            )
            self.audit.append(actor, "stage_started", {"stage": req.stage, "attempt": attempt, "run": run_id}, cid)
        return {"id": run_id, "attempt": attempt}

    def update(self, run_id: str, req: StageRunUpdate, actor: str) -> dict[str, Any]:
        now = to_iso(self.clock.now())
        with db.transaction(self.conn):
            row = db.one(self.conn, "SELECT * FROM stage_runs WHERE id = ?", (run_id,))
            if row is None:
                raise StageRunRefused(404, "not_found", run_id)
            if row["status"] in FINAL:
                raise StageRunRefused(409, "stage_run_final", str(row["status"]))
            sets: dict[str, Any] = {"heartbeat_at": now}
            for name in ("model_actual", "exit_code", "error_class", "error_message", "log_path"):
                value = getattr(req, name)
                if value is not None:
                    sets[name] = value
            if req.status is not None:
                sets["status"] = req.status
                if req.status in FINAL:
                    sets["finished_at"] = now
            assignments = ", ".join(f"{k} = ?" for k in sets)
            self.conn.execute(f"UPDATE stage_runs SET {assignments} WHERE id = ?", (*sets.values(), run_id))  # noqa: S608
            if req.status in FINAL:
                self.audit.append(actor, "stage_finished", {"stage": row["stage"], "run": run_id,
                                                             "status": req.status, "error_class": req.error_class},
                                  str(row["cid"]))  # fmt: skip
        return {"id": run_id, "status": req.status or row["status"]}

    def list(self, status: str | None, cid: str | None, limit: int = 200) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM stage_runs WHERE 1 = 1", []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if cid:
            sql += " AND cid = ?"
            params.append(cid)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(str(limit))
        return [dict(r) for r in self.conn.execute(sql, params)]
