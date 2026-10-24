"""Per-contribution holds (open-discovery design §13.3, ADR-0023, ADR-0025): the web modes' safety net.

sandboxd's egress meter (or the runner) asks for a hold when a fetch/validate job went over its egress
limits (``egress_anomaly``) or, in every mode, when a repo-code job tried to reach a floor (``egress_floor``:
a private, reserved or host address, the owner's public address, an always-denied or repo-code-denied name).
A hold is a stop request (I9): sandbox and worker may set it, only the human releases it (Telegram two-tap
``hold_resume_confirm`` or ``ossctl egress release`` with a TTY confirm). While it is open, ``StageRuns.open``
and ``/package`` answer 423 ``contribution_held`` and ``submit`` stops before the next write (cause ``held``).
It is not a state transition (I13): the contribution keeps its state.

This module is the only SQL writer of ``contribution_holds`` (``tests/unit/gatekeeper/test_holds_writer.py``)
and never imports a Telegram module: the P1 card is queued through the injected ``publish_hold``
callback after the commit (``context.py`` wires it to ``telegram.hold_cards.queue_hold_card``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from typing import Any

from ossbot.common.clock import Clock, to_iso
from ossbot.common.ids import new_id
from ossbot.common.schemas import FloorHoldRequest, HoldRequest, HoldView
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalService
from ossbot.gatekeeper.audit import AuditLog

log = logging.getLogger(__name__)


class HoldRefused(RuntimeError):
    def __init__(self, status: int, code: str, detail: str = "") -> None:
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(f"{status} {code}{': ' + detail if detail else ''}")


def view(row: sqlite3.Row | dict[str, Any]) -> HoldView:
    return HoldView(
        id=str(row["id"]), cid=str(row["cid"]), reason=row["reason"],
        detail=HoldRequest.model_validate(json.loads(row["detail_json"])), held_by=str(row["held_by"]),
        held_at=str(row["held_at"]), released_by=row["released_by"], released_at=row["released_at"],
    )  # fmt: skip


class ContributionHolds:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        audit: AuditLog,
        approvals: ApprovalService,
        publish_hold: Callable[[HoldView], None] = lambda v: None,
    ) -> None:
        self.conn = conn
        self.clock = clock
        self.audit = audit
        self.approvals = approvals
        self.publish_hold = publish_hold

    # ------------------------------------------------------------------- reads
    def _open_row(self, cid: str) -> sqlite3.Row | None:
        return db.one(self.conn, "SELECT * FROM contribution_holds WHERE cid = ? AND released_at IS NULL", (cid,))

    def is_held(self, cid: str) -> bool:
        """Read-only: the check of StageRuns.open, /package and every submit write."""
        return self._open_row(cid) is not None

    def get_open(self, cid: str) -> HoldView | None:
        row = self._open_row(cid)
        return None if row is None else view(row)

    def open_holds(self, *, include_released: bool = False) -> list[HoldView]:
        where = "" if include_released else " WHERE released_at IS NULL"
        rows = self.conn.execute(f"SELECT * FROM contribution_holds{where} ORDER BY held_at, id LIMIT 500")  # noqa: S608
        return [view(r) for r in rows]

    # ------------------------------------------------------------------- writes
    def hold(self, cid: str, req: HoldRequest, actor: str) -> tuple[HoldView, bool]:
        """(the open hold, created). Insert + invalidate this cid's open approvals + audit in one
        transaction; a repeat while one is open returns it (idempotent, audited). The card is queued
        after the commit; a publisher failure is logged and never undoes the hold."""
        now = to_iso(self.clock.now())
        with db.transaction(self.conn):
            if db.one(self.conn, "SELECT 1 FROM contributions WHERE id = ?", (cid,)) is None:
                raise HoldRefused(404, "unknown_cid", cid)
            existing = self._open_row(cid)
            if existing is not None:
                self.audit.append(actor, "contribution_hold_repeat", {"hold": existing["id"], "run": req.run_id}, cid)
                return view(existing), False
            hold_id = new_id("h")
            detail = req.model_dump(mode="json", by_alias=True)  # FloorInfo.cls is "class" on the wire
            self.conn.execute(
                "INSERT INTO contribution_holds (id, cid, reason, detail_json, held_by, held_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (hold_id, cid, req.reason, json.dumps(detail, sort_keys=True), actor, now),
            )
            # egress_anomaly or egress_floor (ADR-0025): both are REASON_LABELS entries
            invalidated = self.approvals.invalidate_contribution(cid, req.reason, actor)
            self.audit.append(actor, "contribution_held", {"hold": hold_id, **detail,
                                                           "invalidated": [i.approval_id for i in invalidated]},
                              cid)  # fmt: skip
            row = self._open_row(cid)
            assert row is not None
            out = view(row)
        if isinstance(req.root, FloorHoldRequest):  # the floor class and the target (escaped), never a breach alone
            what = f"floor reach attempt ({req.root.floor.cls} {req.root.floor.target!r})"
        else:
            what = f"egress anomaly ({','.join(req.root.breached)})"
        log.warning("contribution %s held: %s in %s job %s", cid, what, req.stage, req.run_id,
                    extra={"event": "contribution_held", "cid": cid, "run_id": req.run_id,
                           "reason": req.reason})  # fmt: skip
        try:
            self.publish_hold(out)
        except Exception:
            log.exception("hold card for %s not queued", cid, extra={"event": "hold_card_failed", "cid": cid})
        return out, True

    def release(self, cid: str, actor: str) -> HoldView:
        """Human only (the operator route and the Telegram confirm): the stage may run again."""
        now = to_iso(self.clock.now())
        with db.transaction(self.conn):
            row = self._open_row(cid)
            if row is None:
                raise HoldRefused(404, "no_open_hold", cid)
            self.conn.execute(
                "UPDATE contribution_holds SET released_by = ?, released_at = ? WHERE id = ?", (actor, now, row["id"])
            )
            self.audit.append(actor, "contribution_hold_released", {"hold": row["id"]}, cid)
            released = db.one(self.conn, "SELECT * FROM contribution_holds WHERE id = ?", (row["id"],))
            assert released is not None
        return view(released)
