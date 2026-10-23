"""The per-coder-run network decision for the coder's executor, agent-exec (ADR-0024, design §4b/§4e).

``decide()`` is the gatekeeper half of a two-key check: sandboxd networks agent-exec only when its own
read of the egress-mode flag is ``sandbox_open`` AND this answers ``web``. Check order (it yields exactly
the columns (i) and (i') of the decision table): state, setting, egress mode, target/strict, auto, grant.

The grant (set only by the H-only rerun edge) counts only while ``network_grant = 1`` and
``network_grant_version`` equals the item's current ``proposal_version`` (carried criterion D17); any
other combination is "no grant". Read-only: the POST route adds the audit event.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from ossbot.common.schemas import ImplementEgress, ImplementEgressReason, State
from ossbot.gatekeeper import db

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

Decision = tuple[ImplementEgress, ImplementEgressReason]


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
    if ctx.control.egress_mode(ctx.config.egress.mode_default) != "sandbox_open":
        return "none", "egress_mode"
    target = ctx.discovered.effective_target(str(row["upstream"]))
    if target is None or target.egress == "strict":  # None is unreachable today: fail closed
        return "none", "strict"
    if setting == "auto":
        return "web", "auto"
    if grant_active(row):
        return "web", "rerun"
    return "none", "not_requested"


def decide_and_audit(ctx: AppContext, cid: str, run_id: str, actor: str) -> Decision:
    """sandboxd's per-run decision: the same answer plus ``implement_egress_decided`` for the cid."""
    egress, reason = decide(ctx, cid)
    known = db.one(ctx.conn, "SELECT id FROM contributions WHERE id = ?", (cid,)) is not None
    with db.transaction(ctx.conn):
        ctx.audit.append(actor, "implement_egress_decided", {"run_id": run_id, "egress": egress, "reason": reason},
                         cid=cid if known else None)  # fmt: skip
    return egress, reason
