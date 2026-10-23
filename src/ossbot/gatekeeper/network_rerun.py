"""Rerun with network (ADR-0024, design §4d): the human-only edge out of FAILED.

The terminal FYI's (or the /queue row's) "🌐 Rerun with network", after its confirm tap, calls
``start_rerun``: in one SQLite transaction it re-checks the guard and drives ``FAILED -> PROPOSAL_APPROVED``
with ``reason=network_rerun`` (reserved for the human). The state machine counts the rerun, grants network
for the current proposal version and resets the loop counters. The item reuses its own consumed gate-1
approval and its plan; gate 2 is unchanged. No LLM sits between the tap and the state change (I3, I8).

Only Telegram handlers import this module (import-linter): no API route, Hermes path or feature can reach the
human edge (I9, I13). It imports no Telegram code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.implement_network import rerun_guard
from ossbot.gatekeeper.state_machine import NETWORK_RERUN_REASON, Actor, TransitionError

if TYPE_CHECKING:
    import sqlite3

    from ossbot.gatekeeper.context import AppContext

__all__ = ["NETWORK_RERUN_REASON", "Refusal", "RerunRefused", "RerunStarted", "refusal", "start_rerun"]

Refusal = Literal[
    "not_found", "not_failed", "rerun_reason", "rerun_gate1", "pr_number_null", "rerun_window", "network_reruns",
    "duplicate_active", "held", "strict", "egress_mode", "setting_off", "stopped",
]  # fmt: skip
GUARD_CODES: frozenset[str] = frozenset({
    "rerun_reason", "rerun_gate1", "pr_number_null", "rerun_window", "network_reruns", "duplicate_active", "held",
    "strict", "egress_mode", "setting_off",
})  # fmt: skip


@dataclass(frozen=True)
class RerunStarted:
    cid: str
    n: int  # this rerun's number (1-based)
    max: int


class RerunRefused(Exception):
    def __init__(self, code: Refusal, detail: str = "") -> None:
        self.code: Refusal = code
        super().__init__(f"{code}{': ' + detail if detail else ''}")


def _refusal_of(ctx: AppContext, row: sqlite3.Row | None) -> Refusal | None:
    if row is None:
        return "not_found"
    if row["state"] != State.FAILED.value:
        return "not_failed"
    code = rerun_guard(ctx, row)
    if code is not None:
        return _as_refusal(code)
    if not ctx.approvals.system_running():
        return "stopped"
    return None


def _as_refusal(code: str) -> Refusal:
    if code not in GUARD_CODES:
        raise RuntimeError(f"unmapped rerun guard code {code}")  # a bug, never a silent refusal
    return code  # type: ignore[return-value]


def refusal(ctx: AppContext, cid: str) -> Refusal | None:
    """Read-only pre-check for the button and the confirm card (the state machine re-checks at the tap)."""
    return _refusal_of(ctx, db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,)))


def start_rerun(ctx: AppContext, cid: str, actor: str) -> RerunStarted:
    """The human edge FAILED -> PROPOSAL_APPROVED, all or nothing. ``actor`` is the audit actor of the confirming
    tap (``tg:<user_id>``)."""
    limit = ctx.config.policy.limits.network_reruns_max
    try:
        with db.transaction(ctx.conn):
            row = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
            refused = _refusal_of(ctx, row)
            if refused is not None:
                raise RerunRefused(refused)
            assert row is not None
            n = int(row["network_reruns"]) + 1
            ctx.state_machine.transition(
                cid, State.FAILED, State.PROPOSAL_APPROVED, Actor.HUMAN,
                {"reason": NETWORK_RERUN_REASON, "rerun": n, "max": limit}, actor,
            )  # fmt: skip
    except TransitionError as exc:
        raise RerunRefused(_map(exc), exc.code) from exc
    return RerunStarted(cid, n, limit)


def _map(exc: TransitionError) -> Refusal:
    if exc.status == 423:
        return "stopped"
    if exc.status == 404:
        return "not_found"
    if exc.code == "stale_from":
        return "not_failed"
    if exc.code.startswith("guard_failed:") and exc.code.removeprefix("guard_failed:") in GUARD_CODES:
        return _as_refusal(exc.code.removeprefix("guard_failed:"))
    raise exc  # anything else is a bug, never a silent refusal
