"""Contribution state machine owned by the gatekeeper (DESIGN §9.2, I13).

* ``ALLOWED[from][to]`` is the set of actors that may drive an edge. FAILED edges are generated:
  W|G from every working state, G|H from every gate state (N1).
* Live-PR redirect (M4): with ``pr_number`` set, a move to FAILED/ABANDONED becomes WATCHING
  (except the human WATCHING->ABANDONED), cancelling the fix's open actions and approvals. A redirect
  with ``branch_moved`` also sets ``fixes_disabled_reason``, which refuses every later fix round.
* Owner fix round (ADR-0003 addendum 2026-10-23): WATCHING->IMPLEMENTING is also H (``owner_fix`` only),
  guarded by an open change request and ``owner_fix_rounds < owner_fix_rounds_max``; it counts
  ``owner_fix_rounds``, never ``watch_rounds``. ``reason=owner_fix_round`` is reserved for H (403).
* Guards answer 409 ``guard_failed:<guard>``; a known edge with the wrong actor answers 403.
* Kill/pause is checked on entry to working states (and SUBMITTING); moves that stop work (the
  redirect, into SUBMIT_FAILED, into WATCHING from a gate state) are allowed while paused/killed.
* The transition and its audit event commit in one SQLite transaction; entering IMPLEMENTING
  invalidates the contribution's open approvals in the same transaction.
* CR-004 gate 1: VETTED -> SCOUTING -> PROPOSED (G, inside POST /proposal) -> PROPOSAL_APPROVED (H,
  Start work) -> FETCHING (W, guarded by the consumed approval of the current version and, behind
  gate 1, the claim guard). Decline edges and the two Undo edges are H; RESCOPE_REQUIRED is G.
* Terminal FYI (§14.13.1, §9.10): a move to FAILED or ABANDONED of an item that passed gate 1, not
  redirected and not made by the human, calls the injected ``on_terminal`` hook (the gatekeeper's P2
  FYI, ``telegram/terminal_fyi.py``) inside the transition's transaction, under a savepoint: a failing
  FYI is logged and rolled back alone, never the state change. The hook only reports; it decides nothing.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from ossbot.common.clock import Clock, from_iso, to_iso
from ossbot.common.config import Limits
from ossbot.common.control_flags import Blocked
from ossbot.common.hashing import canonical_json
from ossbot.common.schemas import State, TransitionResponse
from ossbot.gatekeeper import db, outcomes
from ossbot.gatekeeper.approvals import ApprovalService
from ossbot.gatekeeper.audit import AuditLog
from ossbot.gatekeeper.control import ControlService

log = logging.getLogger(__name__)

# (row before the move, from, to, the stored state_reason, the transition evidence) -> None
TerminalHook = Callable[[sqlite3.Row, State, State, str | None, dict[str, Any]], None]


class Actor(StrEnum):
    WORKER = "worker"  # runner via UDS
    GATEKEEPER = "gatekeeper_internal"  # gatekeeper's own code paths (package import, submit, expiry)
    HUMAN = "human"  # Telegram decision or operator TTY-confirmed command


W = frozenset({Actor.WORKER})
G = frozenset({Actor.GATEKEEPER})
H = frozenset({Actor.HUMAN})

S = State
WORKING: frozenset[State] = frozenset(
    {
        S.IMPORTED, S.VETTING, S.FETCHING, S.FETCHED, S.PLANNING, S.PLANNED, S.IMPLEMENTING, S.IMPLEMENTED,
        S.VALIDATING, S.VALIDATION_FAILED, S.VALIDATED, S.REVIEWING, S.REVISE, S.REVIEW_PASSED,
        S.REVIEW_REJECTED, S.PACKAGING, S.CHANGES_REQUESTED, S.WATCHING, S.DISCOVERED, S.VETTED,
        S.SCOUTING, S.PROPOSAL_APPROVED,
    }
)  # fmt: skip
GATE: frozenset[State] = frozenset({S.AWAITING_APPROVAL, S.APPROVED, S.SUBMITTING, S.SUBMITTED, S.SUBMIT_FAILED})
APPROVAL_STATES: frozenset[State] = frozenset(
    {S.AWAITING_APPROVAL, S.APPROVED, S.SUBMITTING, S.SUBMITTED, S.SUBMIT_FAILED, S.DRY_RUN_DONE, S.REJECTED}
)
# CR-004 gate 1 (§9.2): every edge into or out of these is G or H, except the guarded worker edge
# PROPOSAL_APPROVED -> FETCHING and the generated FAILED edge of the working state PROPOSAL_APPROVED.
GATE1_STATES: frozenset[State] = frozenset({S.PROPOSED, S.PROPOSAL_APPROVED, S.PROPOSAL_REJECTED, S.RESCOPE_REQUIRED})
# Waiting states with a worker way out (re-scout); they keep the worker's FAILED edge.
WAITING_W: frozenset[State] = frozenset({S.PROPOSAL_SNOOZED, S.PROPOSAL_EXPIRED})
UNDO_WINDOW = timedelta(minutes=10)  # §14.11.15: Undo of Skip and Later

_LISTED: dict[State, dict[State, frozenset[Actor]]] = {
    S.DISCOVERED: {S.VETTING: W},
    S.VETTING: {S.VETTED: W, S.SKIPPED: W},
    S.VETTED: {S.SCOUTING: W, S.SKIPPED: W | G},  # CR-004: no fetch before gate 1
    S.SCOUTING: {S.PROPOSED: G, S.SKIPPED: W},  # PROPOSED inside POST /proposal after validation
    S.PROPOSED: {S.PROPOSAL_APPROVED: H, S.PROPOSAL_REJECTED: H, S.PROPOSAL_SNOOZED: H, S.PROPOSAL_EXPIRED: G},
    S.PROPOSAL_APPROVED: {S.FETCHING: W, S.SKIPPED: G},  # FETCHING guarded (approval + claim); G = claim rejected
    S.PROPOSAL_REJECTED: {S.PROPOSED: H},  # Undo (Skip only, 10 min)
    S.PROPOSAL_SNOOZED: {S.PROPOSED: H, S.SCOUTING: W},  # Undo (10 min) | snooze elapsed
    S.PROPOSAL_EXPIRED: {S.SCOUTING: W, S.SKIPPED: W},  # still relevant | not
    S.RESCOPE_REQUIRED: {S.PROPOSED: G, S.ABANDONED: H, S.PACKAGING: G},  # new version | Drop | Continue/Re-check
    S.IMPORTED: {S.FETCHING: W},  # M3: creation is G (import endpoint), fetch is W
    S.FETCHING: {S.FETCHED: W},
    S.FETCHED: {S.PLANNING: W, S.IMPLEMENTED: W},  # IMPLEMENTED guarded: source='operator' + unbundle done
    S.PLANNING: {S.PLANNED: W, S.ABANDONED: W},
    S.PLANNED: {S.IMPLEMENTING: W},
    S.IMPLEMENTING: {S.IMPLEMENTED: W},
    S.IMPLEMENTED: {S.VALIDATING: W, S.RESCOPE_REQUIRED: G},
    S.VALIDATING: {S.VALIDATED: W, S.VALIDATION_FAILED: W},
    S.VALIDATION_FAILED: {S.IMPLEMENTING: W},  # attempt < max
    S.VALIDATED: {S.REVIEWING: W, S.RESCOPE_REQUIRED: G},
    S.REVIEWING: {S.REVISE: W, S.REVIEW_REJECTED: W, S.REVIEW_PASSED: W},
    S.REVISE: {S.IMPLEMENTING: W, S.ABANDONED: W},  # round < max
    S.REVIEW_REJECTED: {S.ABANDONED: W},
    S.REVIEW_PASSED: {S.PACKAGING: W, S.RESCOPE_REQUIRED: G},
    S.PACKAGING: {S.AWAITING_APPROVAL: G, S.RESCOPE_REQUIRED: G},  # RESCOPE: /package size check (reapprove|ceiling)
    S.AWAITING_APPROVAL: {
        S.APPROVED: H,
        S.REJECTED: H,
        S.CHANGES_REQUESTED: H,
        S.EXPIRED: G,
        S.WATCHING: H | G,  # WATCHING only when pr_number IS NOT NULL
    },
    S.CHANGES_REQUESTED: {S.IMPLEMENTING: W},
    S.APPROVED: {S.SUBMITTING: G},
    S.SUBMITTING: {S.SUBMITTED: G, S.DRY_RUN_DONE: G, S.SUBMIT_FAILED: G, S.WATCHING: G},
    S.SUBMIT_FAILED: {S.AWAITING_APPROVAL: H, S.WATCHING: H},  # WATCHING only when pr_number set
    S.SUBMITTED: {S.WATCHING: G},
    # IMPLEMENTING H: owner fix round (/fix, 🔧 Fix with notes), owner_fix.start_round only (ADR-0003 addendum)
    S.WATCHING: {S.WATCHING: W, S.IMPLEMENTING: W | H, S.MERGED: W, S.CLOSED: W, S.ABANDONED: H},
}

# Reserved for the human WATCHING->IMPLEMENTING edge: the runner's unbundle rule and the queue label key on it,
# so no other actor may store it as state_reason (I13).
OWNER_FIX_REASON = "owner_fix_round"


def _build_allowed() -> dict[State, dict[State, frozenset[Actor]]]:
    allowed = {k: dict(v) for k, v in _LISTED.items()}
    for state in WORKING | WAITING_W:
        allowed.setdefault(state, {})[S.FAILED] = W | G
    for state in GATE:
        allowed.setdefault(state, {})[S.FAILED] = G | H
    return allowed


ALLOWED: dict[State, dict[State, frozenset[Actor]]] = _build_allowed()

# Entering one of these requires running control state.
BLOCKED_WHILE_STOPPED: frozenset[State] = (WORKING | {S.SUBMITTING}) - {S.WATCHING}
EVIDENCE_MAX_BYTES = 64 * 1024


SHA1 = re.compile(r"^[0-9a-f]{40}$")
REF = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")


def _evidence_columns(from_: State, to: State, evidence: dict[str, Any]) -> dict[str, Any]:
    """Stage results the gatekeeper keeps on the row (the claim guard reads vetting_json; the planner's
    plan and the fetched base are shown on cards). Only these edges, only these keys, size-bounded
    by EVIDENCE_MAX_BYTES; the values stay untrusted data."""
    out: dict[str, Any] = {}
    if (from_, to) == (S.VETTING, S.VETTED) and isinstance(evidence.get("vetting"), dict):
        out["vetting_json"] = canonical_json(evidence["vetting"]).decode("utf-8")
    if (from_, to) == (S.PLANNING, S.PLANNED) and isinstance(evidence.get("plan"), dict):
        out["plan_json"] = canonical_json(evidence["plan"]).decode("utf-8")
    if (from_, to) == (S.FETCHING, S.FETCHED):
        sha, ref = evidence.get("base_sha"), evidence.get("base_ref")
        if isinstance(sha, str) and SHA1.fullmatch(sha) and isinstance(ref, str) and REF.fullmatch(ref):
            out.update(base_sha=sha, base_ref=ref)
    return out


def all_edges() -> list[tuple[State, State, frozenset[Actor]]]:
    return [(f, t, a) for f, targets in ALLOWED.items() for t, a in targets.items()]


class TransitionError(RuntimeError):
    def __init__(self, status: int, code: str, detail: str = "") -> None:
        self.status = status
        self.code = code
        super().__init__(f"{status} {code}{': ' + detail if detail else ''}")


@dataclass(frozen=True)
class Target:
    to: State
    actors: frozenset[Actor]
    redirected_from: State | None


def resolve_target(from_: State, to: State, pr_number: int | None) -> Target:
    """Apply the live-PR redirect (M4). Raises 409 if the requested edge does not exist."""
    actors = ALLOWED.get(from_, {}).get(to)
    if actors is None:
        raise TransitionError(409, "no_edge", f"{from_.value}->{to.value}")
    if pr_number is not None and to in {S.FAILED, S.ABANDONED} and not (from_ is S.WATCHING and to is S.ABANDONED):
        if from_ in WORKING:
            return Target(S.WATCHING, W | G, to)
        if from_ in GATE:
            return Target(S.WATCHING, G | H, to)
    return Target(to, actors, None)


class StateMachine:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        audit: AuditLog,
        approvals: ApprovalService,
        control: ControlService,
        limits: Callable[[], Limits],
        notify: Callable[[str], None] | None = None,
        claim_comment: Callable[[], bool] = lambda: False,
    ) -> None:
        self.conn = conn
        self.clock = clock
        self.audit = audit
        self.approvals = approvals
        self.control = control
        self.limits = limits
        self.notify = notify or (lambda text: None)
        self.claim_comment = claim_comment  # policy proposal.claim_comment == 'on'
        self.on_terminal: TerminalHook | None = None  # set by context.build_context (no Telegram import here)

    # ---------------------------------------------------------------- guards
    def _passed_gate1(self, row: sqlite3.Row) -> bool:
        """The current proposal version's Start work was consumed (same check as the FETCHING guard); a
        re-scout after a snooze or expiry has a version but never passed gate 1, an import has none."""
        if row["proposal_version"] is None:
            return False
        consumed = self.conn.execute(
            "SELECT 1 FROM proposal_approvals WHERE cid = ? AND version = ? AND status = 'consumed' LIMIT 1",
            (row["id"], row["proposal_version"]),
        ).fetchone()
        return consumed is not None

    def _recent_decline(self, row: sqlite3.Row) -> bool:
        at = row["declined_at"]
        return at is not None and self.clock.now() - from_iso(at) <= UNDO_WINDOW

    def _gate1_guard(self, row: sqlite3.Row, from_: State, to: State) -> str | None:
        if from_ is S.PROPOSAL_APPROVED and to is S.FETCHING:
            consumed = self.conn.execute(
                "SELECT 1 FROM proposal_approvals WHERE cid = ? AND version = ? AND status = 'consumed' LIMIT 1",
                (row["id"], row["proposal_version"]),
            ).fetchone()
            if consumed is None:
                return "proposal_approved"
        if from_ is S.PROPOSAL_REJECTED and to is S.PROPOSED:
            if row["declined_reason"] != "skipped":
                return "undo_reason"
            if not self._recent_decline(row):
                return "undo_window"
        if from_ is S.PROPOSAL_SNOOZED and to is S.PROPOSED and not self._recent_decline(row):
            return "undo_window"
        if from_ is S.PROPOSAL_SNOOZED and to is S.SCOUTING:
            until = row["snooze_until"]
            if until is not None and from_iso(until) > self.clock.now():
                return "snoozed"
        return None

    def _owner_fix_guard(self, row: sqlite3.Row, actor: Actor, limits: Limits) -> str | None:
        """WATCHING->IMPLEMENTING: branch_moved disables both actors' rounds (M4); the human edge needs a PR,
        a free owner round (separate from watch_rounds) and an open change request; W keeps watch_rounds."""
        if row["fixes_disabled_reason"] is not None:
            return "fixes_disabled"
        if actor is not Actor.HUMAN:
            return "watch_rounds" if row["watch_rounds"] >= limits.watch_rounds_max else None
        if row["pr_number"] is None:
            return "pr_number_set"
        if row["owner_fix_rounds"] >= limits.owner_fix_rounds_max:
            return "owner_fix_rounds"
        open_request = self.conn.execute(
            "SELECT 1 FROM change_requests WHERE cid = ? AND consumed_at IS NULL LIMIT 1", (row["id"],)
        ).fetchone()
        return None if open_request is not None else "change_request"

    def _guard(self, row: sqlite3.Row, from_: State, to: State, redirected: bool, actor: Actor) -> str | None:
        pr = row["pr_number"]
        gate1 = self._gate1_guard(row, from_, to)
        if gate1 is not None:
            return gate1
        if from_ is S.PROPOSAL_APPROVED and to is S.FETCHING:
            # CR-004: the claim guard sits behind gate 1 (vet found assignment required, or
            # proposal.claim_comment is on); dry-run applies to the claim like any public write.
            # A repo- or PR-level 👤 item has no issue to claim, so the guard does not apply.
            vetting = json.loads(row["vetting_json"] or "{}")
            required = bool(vetting.get("requires_claim_comment") or vetting.get("requires_assignment"))
            if (required or self.claim_comment()) and row["issue_number"] is not None:
                statuses = ["done"] + (["dry_run_done"] if db.current_dry_run(self.conn) else [])
                found = self.conn.execute(
                    "SELECT 1 FROM outbound_actions WHERE cid = ? AND kind = 'issue_claim' AND status IN"
                    f" ({','.join('?' * len(statuses))}) LIMIT 1",
                    (row["id"], *statuses),
                ).fetchone()
                if found is None:
                    return "claim"
        if from_ is S.FETCHED and to is S.IMPLEMENTED:
            if row["source"] != "operator":
                return "source_operator"
            unbundled = self.conn.execute(
                "SELECT 1 FROM stage_runs WHERE cid = ? AND stage = 'unbundle' AND status = 'succeeded' LIMIT 1",
                (row["id"],),
            ).fetchone()
            if unbundled is None:
                return "unbundle_done"
        if from_ is S.AWAITING_APPROVAL and to in {S.REJECTED, S.EXPIRED} and pr is not None:
            return "pr_number_null"
        if not redirected and to is S.WATCHING and from_ in {S.AWAITING_APPROVAL, S.SUBMIT_FAILED} and pr is None:
            return "pr_number_set"
        if from_ is S.SUBMITTING and to is S.DRY_RUN_DONE and pr is not None:
            return "pr_number_null"
        if from_ is S.SUBMITTING and to is S.WATCHING and pr is None:
            return "pr_number_set"
        limits = self.limits()
        if to is S.IMPLEMENTING:
            if from_ is S.VALIDATION_FAILED and row["implement_attempts"] >= limits.max_implement_attempts:
                return "implement_attempts"
            if from_ is S.REVISE and row["review_rounds"] >= limits.max_review_rounds:
                return "review_rounds"
            if from_ is S.CHANGES_REQUESTED and row["change_rounds"] >= limits.max_change_rounds:
                return "change_rounds"
            if from_ is S.WATCHING:
                return self._owner_fix_guard(row, actor, limits)
        if (
            not redirected
            and from_ is S.WATCHING
            and to is S.WATCHING
            and row["watch_rounds"] >= limits.watch_rounds_max
        ):
            return "watch_rounds"
        return None

    @staticmethod
    def _counter_updates(from_: State, to: State, redirected: bool, actor: Actor) -> dict[str, str]:
        if redirected:
            return {}
        if to is S.IMPLEMENTING and from_ is S.WATCHING and actor is Actor.HUMAN:
            return {"owner_fix_rounds": "owner_fix_rounds + 1"}  # an owner round never spends watch_rounds
        if to is S.IMPLEMENTING:
            column = {
                S.VALIDATION_FAILED: "implement_attempts",
                S.REVISE: "review_rounds",
                S.CHANGES_REQUESTED: "change_rounds",
                S.WATCHING: "watch_rounds",
            }.get(from_)
            return {column: f"{column} + 1"} if column else {}
        if from_ is S.WATCHING and to is S.WATCHING:
            return {"watch_rounds": "watch_rounds + 1"}
        return {}

    # ------------------------------------------------------------ transition
    def transition(
        self,
        cid: str,
        from_: State,
        to: State,
        actor: Actor,
        evidence: dict[str, Any] | None = None,
        audit_actor: str | None = None,
    ) -> TransitionResponse:
        evidence = evidence or {}
        if len(canonical_json(evidence)) > EVIDENCE_MAX_BYTES:
            raise TransitionError(400, "evidence_too_large")
        who = audit_actor or actor.value
        forbidden: TransitionError | None = None
        with db.transaction(self.conn):
            row = db.one(self.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
            if row is None:
                raise TransitionError(404, "not_found", cid)
            if row["state"] != from_.value:
                raise TransitionError(409, "stale_from", f"current {row['state']}")
            target = resolve_target(from_, to, row["pr_number"])
            # _apply stores reason or else error_class as state_reason: both are reserved
            claims_owner = OWNER_FIX_REASON in (evidence.get("reason"), evidence.get("error_class"))
            if actor not in target.actors:
                forbidden = TransitionError(403, "forbidden_transition", f"{from_.value}->{to.value} by {actor.value}")
            elif actor is not Actor.HUMAN and claims_owner:
                forbidden = TransitionError(403, "reserved_reason", f"{OWNER_FIX_REASON} is the human's")
            else:
                redirected = target.redirected_from is not None
                guard = self._guard(row, from_, target.to, redirected, actor)
                if guard is not None:
                    raise TransitionError(409, f"guard_failed:{guard}")
                stops_work = redirected or target.to is S.SUBMIT_FAILED or (target.to is S.WATCHING and from_ in GATE)
                if target.to in BLOCKED_WHILE_STOPPED and not stops_work:
                    try:
                        self.control.require_running(f"transition {from_.value}->{target.to.value}")
                    except Blocked as exc:
                        raise TransitionError(423, exc.flags.state, str(exc)) from exc
                self._apply(row, from_, target, evidence, who, actor)
        if forbidden is not None:
            with db.transaction(self.conn):
                self.audit.append(who, "forbidden_transition", {"from": from_.value, "to": to.value}, cid)
            raise forbidden
        return TransitionResponse(state=target.to, redirected_from=target.redirected_from)

    def _apply(
        self, row: sqlite3.Row, from_: State, target: Target, evidence: dict[str, Any], who: str, actor: Actor
    ) -> None:
        cid = str(row["id"])
        error_class = str(evidence.get("error_class", "unknown"))[:64]
        sets = {"state": "?", "state_reason": "?", "updated_at": "?"}
        params: list[Any] = [target.to.value]
        stored_reason: str | None
        if target.redirected_from is not None:
            stored_reason = f"fix_round_failed:{error_class}"
        else:
            reason = evidence.get("reason") or evidence.get("error_class")
            stored_reason = str(reason)[:200] if reason is not None else None
        params.append(stored_reason)
        params.append(to_iso(self.clock.now()))
        redirected = target.redirected_from is not None
        for column, expr in self._counter_updates(from_, target.to, redirected, actor).items():
            sets[column] = expr
        if redirected and error_class == "branch_moved":
            sets["watch_rounds"] = "?"
            params.append(self.limits().watch_rounds_max)
            sets["fixes_disabled_reason"] = "'branch_moved'"  # M4: blocks the owner's rounds too
        if from_ in {S.PROPOSAL_REJECTED, S.PROPOSAL_SNOOZED, S.PROPOSAL_EXPIRED}:
            # Undo or re-scout: the decline record goes with the state (§14.11.15 Undo step 2)
            sets.update({"declined_at": "NULL", "declined_reason": "NULL", "snooze_until": "NULL"})
        for column, value in _evidence_columns(from_, target.to, evidence).items():
            sets[column] = "?"
            params.append(value)
        assignments = ", ".join(f"{k} = {v}" for k, v in sets.items())
        self.conn.execute(f"UPDATE contributions SET {assignments} WHERE id = ?", (*params, cid))
        outcomes.on_transition(self.conn, row, target.to, to_iso(self.clock.now()))
        if target.to is S.IMPLEMENTING:
            self.approvals.invalidate_contribution(cid, "entered_implementing")
        if target.redirected_from is not None:
            self.conn.execute(
                "UPDATE outbound_actions SET status = 'cancelled'"
                " WHERE cid = ? AND status IN ('pending_approval','approved')",
                (cid,),
            )
            self.approvals.invalidate_contribution(cid, "fix_round_failed")
        self.audit.append(
            who,
            "transition",
            {
                "from": from_.value,
                "to": target.to.value,
                "redirected_from": target.redirected_from.value if target.redirected_from else None,
                "evidence": evidence,
            },
            cid,
        )
        if target.redirected_from is not None:
            note = f"Fix for PR #{row['pr_number']} failed ({error_class}); still watching. [Stop watching]"
            if error_class == "branch_moved":
                note = f"branch has commits not made by ossbot; fixes disabled for PR #{row['pr_number']}"
            self.notify(note)
        elif target.to in {S.FAILED, S.ABANDONED} and actor is not Actor.HUMAN and self._passed_gate1(row):
            self._terminal_fyi(row, from_, target.to, stored_reason, evidence)

    def _terminal_fyi(
        self, row: sqlite3.Row, from_: State, to: State, reason: str | None, evidence: dict[str, Any]
    ) -> None:
        """The owner hears of every terminal outcome after gate 1, exactly once (dedupe ``terminal:<cid>``).
        The FYI's rows sit under a savepoint: a failing hook is rolled back alone and logged."""
        if self.on_terminal is None:
            return
        self.conn.execute("SAVEPOINT terminal_fyi")
        try:
            self.on_terminal(row, from_, to, reason, evidence)
        except Exception:
            self.conn.execute("ROLLBACK TO terminal_fyi")
            log.exception("terminal FYI for %s not queued", row["id"],
                          extra={"event": "terminal_fyi_failed", "cid": str(row["id"])})  # fmt: skip
        self.conn.execute("RELEASE terminal_fyi")

    # ------------------------------------------------------------- expiry sweep
    def sweep_expired(self) -> list[str]:
        """Hourly: AWAITING_APPROVAL whose newest approval is > expiry days old and not live (§9.2)."""
        days = self.limits().contribution_expiry_days
        cutoff = self.clock.now() - timedelta(days=days)
        now_iso = to_iso(self.clock.now())
        moved: list[str] = []
        ttl = timedelta(hours=self.limits().approval_ttl_hours)
        for row in self.conn.execute("SELECT * FROM contributions WHERE state = 'AWAITING_APPROVAL'").fetchall():
            # Reference = the newest approval's window start (expires_at - TTL, newest by expires_at), so
            # a re-send that keeps the expiry cannot postpone this sweep (delta 7, §14.18.4.3).
            newest = self.conn.execute(
                "SELECT a.expires_at FROM approvals a JOIN outbound_actions o ON o.id = a.action_id"
                " WHERE o.cid = ? ORDER BY a.expires_at DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            live = self.conn.execute(
                "SELECT 1 FROM approvals a JOIN outbound_actions o ON o.id = a.action_id WHERE o.cid = ?"
                " AND a.status IN ('pending','armed','confirmed') AND a.expires_at > ? LIMIT 1",
                (row["id"], now_iso),
            ).fetchone()
            reference = from_iso(newest["expires_at"]) - ttl if newest else from_iso(row["updated_at"])
            if live is not None or reference > cutoff:
                continue
            to = S.EXPIRED if row["pr_number"] is None else S.WATCHING
            with db.transaction(self.conn):
                if to is S.WATCHING:
                    self.conn.execute(
                        "UPDATE outbound_actions SET status = 'cancelled' WHERE cid = ? AND kind = 'push_update'"
                        " AND status IN ('pending_approval','approved')",
                        (row["id"],),
                    )
                else:  # EXPIRED: nothing of it can be approved any more (no Retry on its S12 cards)
                    self.conn.execute(
                        "UPDATE outbound_actions SET status = 'cancelled'"
                        " WHERE cid = ? AND status = 'pending_approval'",
                        (row["id"],),
                    )
                self.transition(row["id"], S.AWAITING_APPROVAL, to, Actor.GATEKEEPER, {"reason": "expired"})
            moved.append(str(row["id"]))
        return moved
