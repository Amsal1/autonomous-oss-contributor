"""Owner fix rounds on a live PR (DESIGN §9.2, ADR-0003 addendum 2026-10-23).

The owner's ``/fix`` or the status card's 🔧 Fix with notes, after its one-tap confirm, calls
``start_round``: in one SQLite transaction it inserts one ``change_requests`` row (the trusted owner
channel the next implement round reads, consumed by the next package rev) and drives the human edge
``WATCHING -> IMPLEMENTING`` with ``reason=owner_fix_round``. No LLM sits between the tap and the state
change, and nothing public happens before the ``push_update`` card's own two taps (I1, I3, I8).

Only Telegram handlers import this module (import-linter): no API route, Hermes path or feature can
reach the human edge (I9, I13). It imports no Telegram code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ossbot.common.clock import to_iso
from ossbot.common.ids import new_token
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import normalize_text
from ossbot.gatekeeper.state_machine import OWNER_FIX_REASON, Actor, TransitionError

if TYPE_CHECKING:
    import sqlite3

    from ossbot.gatekeeper.context import AppContext

__all__ = ["BOT_INSTRUCTION", "NOTES_MAX", "OWNER_FIX_REASON", "Finding", "FixRefused", "RoundStarted",
           "find_by_pr", "refusal", "start_round"]  # fmt: skip

NOTES_MAX = 4000  # like Request changes (review/comments.COMMENT_MAX)
UNTRUSTED_MAX = 20_000
BOT_INSTRUCTION = "Address this review-bot finding if it is still valid."  # Part B's fixed instruction

Refusal = Literal["not_found", "no_pr", "not_watching", "fixes_disabled", "owner_fix_rounds", "stopped", "empty",
                  "too_long"]  # fmt: skip
_GUARD_CODES: dict[str, Refusal] = {
    "guard_failed:fixes_disabled": "fixes_disabled",
    "guard_failed:owner_fix_rounds": "owner_fix_rounds",
    "guard_failed:pr_number_set": "no_pr",
}


@dataclass(frozen=True)
class Finding:
    """A review-bot finding the gatekeeper re-fetched itself (Part B); its body is untrusted data."""

    comment_id: int
    path: str
    line: int | None
    author: str
    body: str


@dataclass(frozen=True)
class RoundStarted:
    cid: str
    round: int
    max: int
    change_request_id: str
    pr_number: int


class FixRefused(Exception):
    def __init__(self, code: Refusal, detail: str = "") -> None:
        self.code: Refusal = code
        super().__init__(f"{code}{': ' + detail if detail else ''}")


def _refusal_of(ctx: AppContext, row: sqlite3.Row | None) -> Refusal | None:
    if row is None:
        return "not_found"
    if row["pr_number"] is None:
        return "no_pr"
    if row["state"] != State.WATCHING.value:
        return "not_watching"
    if row["fixes_disabled_reason"] is not None:
        return "fixes_disabled"
    if int(row["owner_fix_rounds"]) >= ctx.config.policy.limits.owner_fix_rounds_max:
        return "owner_fix_rounds"
    if not ctx.approvals.system_running():
        return "stopped"
    return None


def refusal(ctx: AppContext, cid: str) -> Refusal | None:
    """Read-only pre-check for the command and the button (the state machine re-checks at the tap)."""
    return _refusal_of(ctx, db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,)))


def find_by_pr(ctx: AppContext, upstream: str, number: int) -> str | None:
    """The newest contribution with that upstream PR, preferring a WATCHING one."""
    row = db.one(
        ctx.conn,
        "SELECT id FROM contributions WHERE upstream = ? COLLATE NOCASE AND pr_number = ?"
        " ORDER BY state = 'WATCHING' DESC, created_at DESC, id DESC LIMIT 1",
        (upstream, number),
    )
    return None if row is None else str(row["id"])


def clean_notes(notes: str) -> str:
    text = normalize_text(notes).strip()
    if not text:
        raise FixRefused("empty")
    if len(text) > NOTES_MAX:
        raise FixRefused("too_long", f"{len(text)} > {NOTES_MAX}")
    return text


def start_round(ctx: AppContext, cid: str, notes: str, actor: str, *, finding: Finding | None = None) -> RoundStarted:
    """One change request plus the human WATCHING->IMPLEMENTING edge, all or nothing. ``actor`` is the
    audit actor of the confirming tap (``tg:<user_id>``)."""
    text = clean_notes(notes)
    request_id = f"r_{new_token()[:12]}"
    source = "owner" if finding is None else "review_bot"
    source_ref = None
    untrusted = None
    if finding is not None:
        source_ref = json.dumps({"comment_id": finding.comment_id, "path": finding.path, "line": finding.line,
                                 "author": finding.author}, sort_keys=True)  # fmt: skip
        untrusted = finding.body[:UNTRUSTED_MAX]
    limit = ctx.config.policy.limits.owner_fix_rounds_max
    try:
        with db.transaction(ctx.conn):
            row = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
            refused = _refusal_of(ctx, row)
            if refused is not None:
                raise FixRefused(refused)
            assert row is not None
            ctx.conn.execute(
                "INSERT INTO change_requests (id, cid, text, created_at, source, source_ref, untrusted_text)"
                " VALUES (?,?,?,?,?,?,?)",
                (request_id, cid, text, to_iso(ctx.clock.now()), source, source_ref, untrusted),
            )
            ctx.state_machine.transition(
                cid, State.WATCHING, State.IMPLEMENTING, Actor.HUMAN,
                {"reason": OWNER_FIX_REASON, "change_request": request_id, "source": source}, actor,
            )  # fmt: skip
    except TransitionError as exc:  # the insert is rolled back with the transaction
        raise FixRefused(_map(exc), exc.code) from exc
    return RoundStarted(cid, int(row["owner_fix_rounds"]) + 1, limit, request_id, int(row["pr_number"]))


def _map(exc: TransitionError) -> Refusal:
    if exc.status == 423:
        return "stopped"
    if exc.status == 404:
        return "not_found"
    if exc.code == "stale_from":
        return "not_watching"
    mapped = _GUARD_CODES.get(exc.code)
    if mapped is None:
        raise exc  # anything else is a bug, never a silent refusal
    return mapped
