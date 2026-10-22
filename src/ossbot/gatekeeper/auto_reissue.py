"""Owed re-sends after an invalidation the owner did not cause (DESIGN §12.4, §14.18.4.3; ADR-0008 addendum).

A card is *owed* when its subject is still waiting on the owner but its newest decision row was
invalidated for one of ``AUTO_REISSUE_REASONS`` (a reload or a start-time snapshot change, a dry-run
toggle, a kill, a binding or text change, a tier switch) and nothing open or confirmed replaced it.
``AppContext.reissue_due`` derives the owed set from the DB on every ``CardPublisher.recover`` (start
and each housekeeping tick) and re-sends through the ``AppContext`` wrappers with the expiry kept, so
the sweep is durable across restarts and idempotent: once a fresh card exists nothing is owed.

Read-only queries here; no Telegram imports. Excluded on purpose: ``expired``, ``live_check:*`` and
``no_write_token`` (explicit 🔁 Retry), newer-version and ``reissued`` reasons (a newer card exists),
work-in-progress reasons, ``render_failed`` and ``binding_unavailable:*`` (would loop), holds
(``egress_anomaly``: Resume first), ``restored`` and rejections.

The window rule (``expires_at > floor``, floor = now + confirm TTL) stops a restart from resurrecting
an old card: an owed card always keeps its original window.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ossbot.common.clock import to_iso
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import reissue_eligible

AUTO_REISSUE_REASONS: frozenset[str] = frozenset(
    {"config_snapshot_changed", "dry_run_toggled", "killed", "binding_changed", "text_edited", "tier_changed"}
)
_AUTO = AUTO_REISSUE_REASONS


@dataclass(frozen=True)
class Owed:
    family: Literal["a", "p", "s"]
    subject: str  # the cid (a, p) or the skill name (s)
    newest_id: str  # the invalidated approval the re-send replaces (the failure memo key, D6)


def held(conn: sqlite3.Connection, cid: str) -> bool:
    return db.one(conn, "SELECT 1 FROM contribution_holds WHERE cid = ? AND released_at IS NULL", (cid,)) is not None


def _state(value: str) -> State | None:
    try:
        return State(value)
    except ValueError:
        return None


def owed_actions(conn: sqlite3.Connection, now: datetime, floor: datetime, *, cid: str | None = None) -> list[Owed]:
    """One entry per cid with at least one owed outbound action (``ApprovalService.reissue`` is per cid).
    ``now`` is kept for the call shape; the window floor already carries now + confirm TTL."""
    rows = conn.execute(
        "SELECT o.id, o.cid, o.kind, o.status, c.state FROM outbound_actions o JOIN contributions c ON c.id = o.cid"
        " WHERE o.status = 'pending_approval' AND (? IS NULL OR o.cid = ?)"
        " AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.action_id = o.id"
        " AND a.status IN ('pending','armed','confirmed'))"
        " AND NOT EXISTS (SELECT 1 FROM approvals a2 JOIN outbound_actions o2 ON o2.id = a2.action_id"
        " WHERE o2.cid = o.cid AND a2.status = 'confirmed')"
        " AND NOT EXISTS (SELECT 1 FROM contribution_holds h WHERE h.cid = o.cid AND h.released_at IS NULL)"
        " ORDER BY o.created_at, o.id",
        (cid, cid),
    ).fetchall()
    out: dict[str, Owed] = {}
    for r in rows:
        state = _state(str(r["state"]))
        if state is None or not reissue_eligible(r["kind"], str(r["status"]), state) or str(r["cid"]) in out:
            continue
        newest = db.one(conn, "SELECT id, status, invalidated_reason, expires_at FROM approvals WHERE action_id = ?"
                        " ORDER BY created_at DESC, rowid DESC LIMIT 1", (r["id"],))  # fmt: skip
        if newest is None or newest["status"] != "invalidated":
            continue
        if newest["invalidated_reason"] not in AUTO_REISSUE_REASONS or str(newest["expires_at"]) <= to_iso(floor):
            continue
        out[str(r["cid"])] = Owed("a", str(r["cid"]), str(newest["id"]))
    return list(out.values())


def owed_proposals(conn: sqlite3.Connection, now: datetime, floor: datetime, *, cid: str | None = None) -> list[Owed]:
    """``PROPOSED`` contributions whose newest proposal card was auto-invalidated and not replaced."""
    rows = conn.execute(
        "SELECT c.id FROM contributions c WHERE c.state = 'PROPOSED' AND (? IS NULL OR c.id = ?)"
        " AND NOT EXISTS (SELECT 1 FROM proposal_approvals p WHERE p.cid = c.id"
        " AND p.status IN ('pending','armed','confirmed'))"
        " AND NOT EXISTS (SELECT 1 FROM contribution_holds h WHERE h.cid = c.id AND h.released_at IS NULL)"
        " ORDER BY c.created_at, c.id",
        (cid, cid),
    ).fetchall()
    out: list[Owed] = []
    for r in rows:
        newest = db.one(conn, "SELECT id, status, invalidated_reason, expires_at FROM proposal_approvals"
                        " WHERE cid = ? ORDER BY version DESC, created_at DESC, rowid DESC LIMIT 1",
                        (r["id"],))  # fmt: skip
        if newest is None or newest["status"] != "invalidated":
            continue
        if newest["invalidated_reason"] in _AUTO and str(newest["expires_at"]) > to_iso(floor):
            out.append(Owed("p", str(r["id"]), str(newest["id"])))
    return out


def owed_skills(conn: sqlite3.Connection, now: datetime, floor: datetime, *, skill: str | None = None) -> list[Owed]:
    """Skills whose newest card was auto-invalidated and not replaced (the service checks staged + tier)."""
    names = [str(r[0]) for r in conn.execute("SELECT DISTINCT skill FROM skill_approvals WHERE (? IS NULL OR"
                                             " skill = ?) ORDER BY skill", (skill, skill))]  # fmt: skip
    out: list[Owed] = []
    for name in names:
        if db.one(conn, "SELECT 1 FROM skill_approvals WHERE skill = ? AND status IN ('pending','armed','confirmed')",
                  (name,)) is not None:  # fmt: skip
            continue
        newest = db.one(conn, "SELECT id, status, invalidated_reason, expires_at FROM skill_approvals WHERE skill = ?"
                        " ORDER BY created_at DESC, rowid DESC LIMIT 1", (name,))  # fmt: skip
        if newest is None or newest["status"] != "invalidated":
            continue
        if newest["invalidated_reason"] in _AUTO and str(newest["expires_at"]) > to_iso(floor):
            out.append(Owed("s", name, str(newest["id"])))
    return out


def owed(conn: sqlite3.Connection, now: datetime, floor: datetime) -> list[Owed]:
    """Everything owed, gate 2 first (the order the sweep re-sends in)."""
    return [*owed_actions(conn, now, floor), *owed_proposals(conn, now, floor), *owed_skills(conn, now, floor)]


def owed_for(conn: sqlite3.Connection, now: datetime, floor: datetime, *, cid: str | None = None,
             skill: str | None = None) -> bool:  # fmt: skip
    """The stale card's forward rule (§14.11.12): a re-send is owed for this subject (running or not)."""
    if skill is not None:
        return bool(owed_skills(conn, now, floor, skill=skill))
    if cid is None:
        return False
    return bool(owed_actions(conn, now, floor, cid=cid) or owed_proposals(conn, now, floor, cid=cid))
