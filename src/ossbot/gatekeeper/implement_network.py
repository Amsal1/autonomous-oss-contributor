"""The per-coder-run network decision for the coder's executor, agent-exec (ADR-0024, design §4b/§4e).

``decide()`` is the gatekeeper half of a two-key check: sandboxd networks agent-exec only when its own
read of the egress-mode flag is a code-web mode (``sandbox_open`` or ``sandbox_full``, ADR-0025) AND this
answers ``web``. Check order (it yields exactly
the columns (i) and (i') of the decision table): state, setting, egress mode, target/strict, auto, grant.

The grant (set only by the H-only rerun edge) counts only while ``network_grant = 1`` and
``network_grant_version`` equals the item's current ``proposal_version`` (carried criterion D17); any
other combination is "no grant". Read-only: the POST route adds the audit event.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import TYPE_CHECKING

from ossbot.common.clock import from_iso
from ossbot.common.control_flags import CODE_WEB_MODES
from ossbot.common.schemas import TERMINAL_STATES, ImplementEgress, ImplementEgressReason, State
from ossbot.gatekeeper import db

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

Decision = tuple[ImplementEgress, ImplementEgressReason]
# ≤ the minimum retention.contributions_days (7), so nothing a rerun needs is gone and a rerun-able row is never
# eligible for retention (the two intervals are disjoint, design §4d)
RERUN_WINDOW = timedelta(days=7)
TERMINAL = tuple(sorted(s.value for s in TERMINAL_STATES))


def grant_active(row: sqlite3.Row) -> bool:
    """The rerun grant, bound to the proposal version it was tapped for (C-D17)."""
    version = row["network_grant_version"]
    return (int(row["network_grant"]) == 1 and version is not None
            and row["proposal_version"] is not None and int(version) == int(row["proposal_version"]))  # fmt: skip


def decide(ctx: AppContext, cid: str) -> Decision:
    row = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
    if row is None:
        return "none", "unknown_cid"
    if row["state"] != State.IMPLEMENTING.value:
        return "none", "not_implementing"
    setting = ctx.control.implement_network()
    if setting == "off":
        return "none", "setting_off"
    if ctx.control.egress_mode(ctx.config.egress.mode_default) not in CODE_WEB_MODES:
        return "none", "egress_mode"
    target = ctx.discovered.effective_target(str(row["upstream"]))
    if target is None or target.egress == "strict":  # None is unreachable today: fail closed
        return "none", "strict"
    if setting == "auto":
        return "web", "auto"
    if grant_active(row):
        return "web", "rerun"
    return "none", "not_requested"


def passed_gate1(ctx: AppContext, row: sqlite3.Row) -> bool:
    """The item's own consumed gate-1 approval of its current proposal version (the FETCHING guard's check)."""
    if row["proposal_version"] is None:
        return False
    found = db.one(ctx.conn, "SELECT 1 AS x FROM proposal_approvals WHERE cid = ? AND version = ? AND status ="
                   " 'consumed' LIMIT 1", (row["id"], row["proposal_version"]))  # fmt: skip
    return found is not None


def item_guard(ctx: AppContext, row: sqlite3.Row) -> str | None:
    """The item part of the rerun guard (design §4d, codes ``rerun_reason`` … ``held``): whether a rerun could
    ever be offered for this FAILED row, independent of the repo and the owner's switches."""
    if row["state_reason"] != "needs_network" and int(row["rerun_hint"]) != 1:
        return "rerun_reason"
    if row["source"] == "operator" or not passed_gate1(ctx, row):
        return "rerun_gate1"
    if row["pr_number"] is not None:
        return "pr_number_null"
    if ctx.clock.now() - from_iso(str(row["updated_at"])) > RERUN_WINDOW:
        return "rerun_window"
    if int(row["network_reruns"]) >= ctx.config.policy.limits.network_reruns_max:
        return "network_reruns"
    if row["issue_number"] is not None:
        marks = ",".join("?" * len(TERMINAL))  # placeholders only; the values are bound
        sql = f"SELECT id FROM contributions WHERE upstream = ? AND issue_number = ? AND id != ? AND state NOT IN ({marks}) LIMIT 1"  # noqa: E501, S608
        other = db.one(ctx.conn, sql, (row["upstream"], row["issue_number"], row["id"], *TERMINAL))
        if other is not None:
            return "duplicate_active"
    if ctx.holds.is_held(str(row["id"])):
        return "held"
    return None


def repo_strict(ctx: AppContext, upstream: str) -> bool:
    """``egress: strict`` (config first, then the DB-kept repo); an unresolvable target counts as strict."""
    target = ctx.discovered.effective_target(upstream)
    return target is None or target.egress == "strict"


def rerun_guard(ctx: AppContext, row: sqlite3.Row) -> str | None:
    """The first failing code of design §4d for the H-only edge FAILED -> PROPOSAL_APPROVED (the state machine's
    injected guard and the Telegram pre-check). None = the rerun may start (the running check comes after)."""
    code = item_guard(ctx, row)
    if code is not None:
        return code
    if repo_strict(ctx, str(row["upstream"])):
        return "strict"
    if ctx.control.egress_mode(ctx.config.egress.mode_default) not in CODE_WEB_MODES:
        return "egress_mode"
    if ctx.control.implement_network() == "off":
        return "setting_off"
    return None


def decide_and_audit(ctx: AppContext, cid: str, run_id: str, actor: str) -> Decision:
    """sandboxd's per-run decision: the same answer plus ``implement_egress_decided`` for the cid."""
    egress, reason = decide(ctx, cid)
    known = db.one(ctx.conn, "SELECT id FROM contributions WHERE id = ?", (cid,)) is not None
    with db.transaction(ctx.conn):
        ctx.audit.append(actor, "implement_egress_decided", {"run_id": run_id, "egress": egress, "reason": reason},
                         cid=cid if known else None)  # fmt: skip
    return egress, reason
