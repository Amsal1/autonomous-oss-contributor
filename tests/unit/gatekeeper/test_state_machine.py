"""State machine: actor table, generated FAILED edges, guards, counters, live-PR redirect (§9.2, §22.1)."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import timedelta

import pytest

from ossbot.common.clock import FakeClock, to_iso
from ossbot.common.schemas import TERMINAL_STATES, State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.state_machine import (
    ALLOWED,
    APPROVAL_STATES,
    GATE,
    GATE1_STATES,
    WORKING,
    Actor,
    TransitionError,
    all_edges,
)
from ossbot.gatekeeper.telegram import terminal_fyi
from tests.conftest import (
    consumed_proposal,
    insert_action,
    insert_contribution,
    packaged_contribution,
    write_flag,
)

S = State
EDGES = all_edges()


def guard_columns(from_: State, to: State) -> dict[str, object]:
    """Row values that satisfy the guards of an edge (so the actor check is what is tested)."""
    cols: dict[str, object] = {}
    if to is S.WATCHING and from_ in {S.AWAITING_APPROVAL, S.SUBMIT_FAILED, S.SUBMITTING}:
        cols["pr_number"] = 7
    if from_ is S.FETCHED and to is S.IMPLEMENTED:
        cols["source"] = "operator"
    if from_ in {S.PROPOSAL_REJECTED, S.PROPOSAL_SNOOZED} and to is S.PROPOSED:
        cols.update(declined_at=to_iso(FakeClock().now()), declined_reason="skipped")
    if from_ is S.WATCHING and to is S.IMPLEMENTING:
        cols["pr_number"] = 7
    return cols


def change_request(ctx: AppContext, cid: str, *, consumed: bool = False) -> str:
    """An owner change request (the human WATCHING -> IMPLEMENTING edge needs an open one)."""
    rid = f"r_{cid[2:]}"
    with db.transaction(ctx.conn):
        ctx.conn.execute("INSERT INTO change_requests (id, cid, text, created_at, consumed_at) VALUES (?,?,?,?,?)",
                         (rid, cid, "fix it", "t", "t" if consumed else None))  # fmt: skip
    return rid


def prepare_edge(ctx: AppContext, cid: str, from_: State, to: State, actor: Actor) -> None:
    """Rows besides the contribution's own columns that an edge's guard needs."""
    if (from_, to, actor) == (S.WATCHING, S.IMPLEMENTING, Actor.HUMAN):
        change_request(ctx, cid)


def make(ctx: AppContext, state: State, **cols: object) -> str:
    source = str(cols.pop("source", "discover"))
    if state is S.PROPOSAL_APPROVED:
        cols.setdefault("proposal_version", 1)
    cid = insert_contribution(ctx, state, source=source, **cols)
    if state is S.PROPOSAL_APPROVED:
        consumed_proposal(ctx, cid)
    if source == "operator" and state is S.FETCHED:
        with db.transaction(ctx.conn):
            ctx.conn.execute(
                "INSERT INTO stage_runs (id, cid, stage, attempt, status) VALUES (?, ?, 'unbundle', 1, 'succeeded')",
                (f"r_{cid}", cid),
            )
    return cid


def state_of(ctx: AppContext, cid: str) -> str:
    row = db.one(ctx.conn, "SELECT state FROM contributions WHERE id = ?", (cid,))
    assert row is not None
    return str(row["state"])


@pytest.mark.parametrize(("from_", "to", "actors"), EDGES, ids=[f"{f}->{t}" for f, t, _ in EDGES])
def test_every_allowed_edge_succeeds_for_its_actors(
    ctx: AppContext, from_: State, to: State, actors: frozenset[Actor]
) -> None:
    for actor in sorted(actors):
        cid = make(ctx, from_, **guard_columns(from_, to))
        prepare_edge(ctx, cid, from_, to, actor)
        result = ctx.state_machine.transition(cid, from_, to, actor)
        assert result.redirected_from is None
        assert state_of(ctx, cid) == to.value


@pytest.mark.parametrize(("from_", "to", "actors"), EDGES, ids=[f"{f}->{t}" for f, t, _ in EDGES])
def test_wrong_actor_gets_403_without_state_change(
    ctx: AppContext, from_: State, to: State, actors: frozenset[Actor]
) -> None:
    for actor in set(Actor) - actors:
        cid = make(ctx, from_, **guard_columns(from_, to))
        with pytest.raises(TransitionError) as info:
            ctx.state_machine.transition(cid, from_, to, actor)
        assert info.value.status == 403
        assert state_of(ctx, cid) == from_.value


APPROVAL_EDGES = [
    (f, t) for f, t, _ in EDGES if f in APPROVAL_STATES or t in APPROVAL_STATES or t is S.CHANGES_REQUESTED
]


@pytest.mark.parametrize(("from_", "to"), APPROVAL_EDGES, ids=[f"{f}->{t}" for f, t in APPROVAL_EDGES])
def test_worker_cannot_drive_approval_states(ctx: AppContext, from_: State, to: State) -> None:
    """AC 19: every edge touching the approval states (and into CHANGES_REQUESTED) is 403 for W."""
    cid = make(ctx, from_, **guard_columns(from_, to))
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid, from_, to, Actor.WORKER)
    assert info.value.status == 403
    assert state_of(ctx, cid) == from_.value
    forbidden = ctx.conn.execute("SELECT COUNT(*) FROM audit_events WHERE event = 'forbidden_transition'").fetchone()
    assert forbidden[0] == 1


def test_approval_edges_cover_the_gate() -> None:
    assert len(APPROVAL_EDGES) >= 15
    assert all(Actor.WORKER not in ALLOWED[f][t] for f, t in APPROVAL_EDGES)


NON_EDGES = [(f, t) for f in S for t in S if t not in ALLOWED.get(f, {})]


@pytest.mark.parametrize(("from_", "to"), NON_EDGES[::7])
def test_non_edges_return_409(ctx: AppContext, from_: State, to: State) -> None:
    cid = make(ctx, from_)
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid, from_, to, Actor.GATEKEEPER)
    assert info.value.status == 409 and info.value.code == "no_edge"


def test_imported_to_validating_is_not_an_edge(ctx: AppContext) -> None:
    cid = make(ctx, S.IMPORTED, source="operator")
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid, S.IMPORTED, S.VALIDATING, Actor.WORKER)
    assert info.value.status == 409


def test_failed_edges_generated() -> None:
    for state in WORKING:
        assert ALLOWED[state][S.FAILED] == {Actor.WORKER, Actor.GATEKEEPER}
    for state in GATE:
        assert ALLOWED[state][S.FAILED] == {Actor.GATEKEEPER, Actor.HUMAN}


def test_worker_failing_a_gate_state_is_403(ctx: AppContext) -> None:
    cid = make(ctx, S.SUBMIT_FAILED)
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid, S.SUBMIT_FAILED, S.FAILED, Actor.WORKER)
    assert info.value.status == 403


def test_vetted_to_skipped_by_worker_and_gatekeeper(ctx: AppContext) -> None:
    for actor in (Actor.WORKER, Actor.GATEKEEPER):
        cid = make(ctx, S.VETTED)
        ctx.state_machine.transition(cid, S.VETTED, S.SKIPPED, actor, {"reason": "claim_rejected"})
        assert state_of(ctx, cid) == "SKIPPED"


def test_stale_from_is_409(ctx: AppContext) -> None:
    cid = make(ctx, S.FETCHING)
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid, S.DISCOVERED, S.VETTING, Actor.WORKER)
    assert info.value.status == 409 and info.value.code == "stale_from"


def test_unknown_contribution_is_404(ctx: AppContext) -> None:
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition("c_0000000000", S.DISCOVERED, S.VETTING, Actor.WORKER)
    assert info.value.status == 404


# ----------------------------------------------------------------------------- guards
def test_no_fetch_before_gate_1(ctx: AppContext) -> None:
    """CR-004 AC 37: VETTED goes to SCOUTING, never straight to FETCHING."""
    cid = make(ctx, S.VETTED)
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid, S.VETTED, S.FETCHING, Actor.WORKER)
    assert info.value.code == "no_edge"


@pytest.mark.parametrize("status", ["pending", "armed", "confirmed", "invalidated", "expired", "rejected"])
def test_fetch_needs_the_consumed_approval_of_the_current_version(ctx: AppContext, status: str) -> None:
    cid = insert_contribution(ctx, S.PROPOSAL_APPROVED, proposal_version=2)
    consumed_proposal(ctx, cid, version=1)  # an older version's Start work does not count
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO proposal_approvals (id, cid, version, binding_json, binding_hash, status, tg_chat_id,"
            " created_at, expires_at) VALUES ('p_X000000002', ?, 2, '{}', 'h', ?, 1, 't', 't')",
            (cid, status),
        )
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid, S.PROPOSAL_APPROVED, S.FETCHING, Actor.WORKER)
    assert info.value.code == "guard_failed:proposal_approved"


@pytest.mark.parametrize("vetting", [{"requires_claim_comment": True}, {"requires_assignment": True}, None])
def test_claim_guard_sits_behind_gate_1(ctx: AppContext, vetting: dict[str, bool] | None) -> None:
    """Claim after gate 1 (CR4-1h): vet's requirement or proposal.claim_comment: on; dry-run applies."""
    if vetting is None:
        ctx.state_machine.claim_comment = lambda: True
    cid = make(ctx, S.PROPOSAL_APPROVED, vetting_json=json.dumps(vetting or {}))
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid, S.PROPOSAL_APPROVED, S.FETCHING, Actor.WORKER)
    assert info.value.code == "guard_failed:claim"
    # dry-run (default ON): a dry_run_done claim satisfies the guard
    insert_action(ctx, cid, "issue_claim", {"target_number": 101, "body": "x"}, status="dry_run_done")
    ctx.state_machine.transition(cid, S.PROPOSAL_APPROVED, S.FETCHING, Actor.WORKER)


def test_claim_guard_live_needs_done(ctx: AppContext) -> None:
    ctx.control.set_dry_run(False, "tg:4242")
    cid = make(ctx, S.PROPOSAL_APPROVED, vetting_json=json.dumps({"requires_claim_comment": True}))
    insert_action(ctx, cid, "issue_claim", {"target_number": 101, "body": "x"}, status="dry_run_done")
    with pytest.raises(TransitionError):
        ctx.state_machine.transition(cid, S.PROPOSAL_APPROVED, S.FETCHING, Actor.WORKER)
    insert_action(ctx, cid, "issue_claim", {"target_number": 101, "body": "x"}, status="done")
    ctx.state_machine.transition(cid, S.PROPOSAL_APPROVED, S.FETCHING, Actor.WORKER)


def test_claim_rejected_after_gate_1_skips(ctx: AppContext) -> None:
    cid = make(ctx, S.PROPOSAL_APPROVED)
    ctx.state_machine.transition(cid, S.PROPOSAL_APPROVED, S.SKIPPED, Actor.GATEKEEPER, {"reason": "claim_rejected"})
    assert state_of(ctx, cid) == "SKIPPED"


GATE1_EDGES = [
    (f, t)
    for f, t, _ in EDGES
    if (f in GATE1_STATES or t in GATE1_STATES)
    and (f, t) not in {(S.PROPOSAL_APPROVED, S.FETCHING), (S.PROPOSAL_APPROVED, S.FAILED)}
]


@pytest.mark.parametrize(("from_", "to"), GATE1_EDGES, ids=[f"{f}->{t}" for f, t in GATE1_EDGES])
def test_worker_cannot_drive_gate_1(ctx: AppContext, from_: State, to: State) -> None:
    """AC 19 extended (CR-004): edges into or out of the gate-1 states are G or H only."""
    assert Actor.WORKER not in ALLOWED[from_][to]
    cid = make(ctx, from_, **guard_columns(from_, to))
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid, from_, to, Actor.WORKER)
    assert info.value.status == 403 and state_of(ctx, cid) == from_.value


def test_gate_1_edges_and_actors() -> None:
    assert ALLOWED[S.VETTED] == {S.SCOUTING: {Actor.WORKER}, S.SKIPPED: {Actor.WORKER, Actor.GATEKEEPER},
                                 S.FAILED: {Actor.WORKER, Actor.GATEKEEPER}}  # fmt: skip
    assert ALLOWED[S.SCOUTING][S.PROPOSED] == {Actor.GATEKEEPER}
    for to in (S.PROPOSAL_APPROVED, S.PROPOSAL_REJECTED, S.PROPOSAL_SNOOZED):
        assert ALLOWED[S.PROPOSED][to] == {Actor.HUMAN}
    assert ALLOWED[S.PROPOSED][S.PROPOSAL_EXPIRED] == {Actor.GATEKEEPER}
    assert ALLOWED[S.PROPOSAL_REJECTED] == {S.PROPOSED: {Actor.HUMAN}}  # terminal unless Undo
    assert ALLOWED[S.PROPOSAL_SNOOZED][S.PROPOSED] == {Actor.HUMAN}
    for src in (S.IMPLEMENTED, S.VALIDATED, S.REVIEW_PASSED, S.PACKAGING):
        assert ALLOWED[src][S.RESCOPE_REQUIRED] == {Actor.GATEKEEPER}
    assert ALLOWED[S.RESCOPE_REQUIRED][S.ABANDONED] == {Actor.HUMAN}
    assert S.PROPOSAL_REJECTED in TERMINAL_STATES and S.PROPOSED not in TERMINAL_STATES


@pytest.mark.parametrize("from_", [S.PROPOSAL_REJECTED, S.PROPOSAL_SNOOZED])
def test_undo_edges(ctx: AppContext, clock: FakeClock, from_: State) -> None:
    """§14.11.15: human only, ≤ 10 min, Skip only for PROPOSAL_REJECTED; the decline record is cleared."""
    cols = guard_columns(from_, S.PROPOSED)
    cid = make(ctx, from_, **cols)
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid, from_, S.PROPOSED, Actor.GATEKEEPER)
    assert info.value.status == 403
    late = make(ctx, from_, declined_at=to_iso(clock.now() - timedelta(minutes=11)), declined_reason="skipped")
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(late, from_, S.PROPOSED, Actor.HUMAN)
    assert info.value.code == "guard_failed:undo_window"
    ctx.state_machine.transition(cid, from_, S.PROPOSED, Actor.HUMAN)
    row = db.one(ctx.conn, "SELECT state, declined_at, declined_reason FROM contributions WHERE id = ?", (cid,))
    assert row is not None and tuple(row) == ("PROPOSED", None, None)


@pytest.mark.parametrize("reason", ["never_repo", "never_kind", "snoozed"])
def test_undo_refused_for_never(ctx: AppContext, clock: FakeClock, reason: str) -> None:
    cid = make(ctx, S.PROPOSAL_REJECTED, declined_at=to_iso(clock.now()), declined_reason=reason)
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid, S.PROPOSAL_REJECTED, S.PROPOSED, Actor.HUMAN)
    assert info.value.code == "guard_failed:undo_reason"


def test_snooze_elapsed_rescouts(ctx: AppContext, clock: FakeClock) -> None:
    cid = make(ctx, S.PROPOSAL_SNOOZED, snooze_until=to_iso(clock.now() + timedelta(days=1)))
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid, S.PROPOSAL_SNOOZED, S.SCOUTING, Actor.WORKER)
    assert info.value.code == "guard_failed:snoozed"
    clock.advance(hours=25)
    ctx.state_machine.transition(cid, S.PROPOSAL_SNOOZED, S.SCOUTING, Actor.WORKER)
    assert state_of(ctx, cid) == "SCOUTING"


def test_fetched_to_implemented_requires_operator_and_unbundle(ctx: AppContext) -> None:
    cid = make(ctx, S.FETCHED)
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid, S.FETCHED, S.IMPLEMENTED, Actor.WORKER)
    assert info.value.code == "guard_failed:source_operator"
    cid2 = insert_contribution(ctx, S.FETCHED, source="operator", issue_number=None)
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid2, S.FETCHED, S.IMPLEMENTED, Actor.WORKER)
    assert info.value.code == "guard_failed:unbundle_done"
    cid3 = make(ctx, S.FETCHED, source="operator", issue_number=None)
    ctx.state_machine.transition(cid3, S.FETCHED, S.IMPLEMENTED, Actor.WORKER)


@pytest.mark.parametrize(
    ("from_", "to", "actor", "pr", "code"),
    [
        (S.AWAITING_APPROVAL, S.REJECTED, Actor.HUMAN, 7, "guard_failed:pr_number_null"),
        (S.AWAITING_APPROVAL, S.EXPIRED, Actor.GATEKEEPER, 7, "guard_failed:pr_number_null"),
        (S.AWAITING_APPROVAL, S.WATCHING, Actor.HUMAN, None, "guard_failed:pr_number_set"),
        (S.SUBMIT_FAILED, S.WATCHING, Actor.HUMAN, None, "guard_failed:pr_number_set"),
        (S.SUBMITTING, S.DRY_RUN_DONE, Actor.GATEKEEPER, 7, "guard_failed:pr_number_null"),
        (S.SUBMITTING, S.WATCHING, Actor.GATEKEEPER, None, "guard_failed:pr_number_set"),
    ],
)
def test_fix_round_pr_guards(ctx: AppContext, from_: State, to: State, actor: Actor, pr: int | None, code: str) -> None:
    cid = make(ctx, from_, pr_number=pr)
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid, from_, to, actor)
    assert info.value.status == 409 and info.value.code == code
    assert state_of(ctx, cid) == from_.value


@pytest.mark.parametrize(
    ("from_", "column", "limit_attr"),
    [
        (S.VALIDATION_FAILED, "implement_attempts", "max_implement_attempts"),
        (S.REVISE, "review_rounds", "max_review_rounds"),
        (S.CHANGES_REQUESTED, "change_rounds", "max_change_rounds"),
        (S.WATCHING, "watch_rounds", "watch_rounds_max"),
    ],
)
def test_loop_counters_enforce_maxima(ctx: AppContext, from_: State, column: str, limit_attr: str) -> None:
    limit = getattr(ctx.config.policy.limits, limit_attr)
    cid = make(ctx, from_, pr_number=7 if from_ is S.WATCHING else None)
    for i in range(limit):
        ctx.state_machine.transition(cid, from_, S.IMPLEMENTING, Actor.WORKER)
        row = db.one(ctx.conn, f"SELECT {column} FROM contributions WHERE id = ?", (cid,))
        assert row is not None and row[0] == i + 1
        with db.transaction(ctx.conn):
            ctx.conn.execute("UPDATE contributions SET state = ? WHERE id = ?", (from_.value, cid))
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid, from_, S.IMPLEMENTING, Actor.WORKER)
    assert info.value.code == f"guard_failed:{column}"


def test_watching_self_loop_counts_rounds(ctx: AppContext) -> None:
    cid = make(ctx, S.WATCHING, pr_number=7)
    for _ in range(ctx.config.policy.limits.watch_rounds_max):
        ctx.state_machine.transition(cid, S.WATCHING, S.WATCHING, Actor.WORKER)
    with pytest.raises(TransitionError):
        ctx.state_machine.transition(cid, S.WATCHING, S.WATCHING, Actor.WORKER)


def test_entering_implementing_invalidates_approvals(ctx: AppContext) -> None:
    cid, action = packaged_contribution(ctx)
    approval = ctx.approvals.create(action, tg_chat_id=4242, body_parts_total=1)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE contributions SET state = 'CHANGES_REQUESTED' WHERE id = ?", (cid,))
    ctx.state_machine.transition(cid, S.CHANGES_REQUESTED, S.IMPLEMENTING, Actor.WORKER)
    row = db.one(ctx.conn, "SELECT status, invalidated_reason FROM approvals WHERE id = ?", (approval,))
    assert row is not None and row["status"] == "invalidated" and row["invalidated_reason"] == "entered_implementing"


# -------------------------------------------------------------------- live-PR redirect (M4)
REDIRECT_CASES = [
    ("validation_attempts_exhausted", S.VALIDATION_FAILED, S.FAILED),
    ("revise_rounds_exhausted", S.REVISE, S.ABANDONED),
    ("review_rejected", S.REVIEW_REJECTED, S.ABANDONED),
    ("patch_mismatch", S.PACKAGING, S.FAILED),
    ("bundle_refs", S.PACKAGING, S.FAILED),
    ("bundle_prereq_missing", S.IMPLEMENTING, S.FAILED),
]


@pytest.mark.parametrize(("error_class", "from_", "to"), REDIRECT_CASES)
def test_fix_round_failure_returns_to_watching(ctx: AppContext, error_class: str, from_: State, to: State) -> None:
    notes: list[str] = []
    ctx.state_machine.notify = notes.append
    cid = make(ctx, from_, pr_number=7)
    pending = insert_action(ctx, cid, "comment", {"target_number": 7, "body": "x"})
    result = ctx.state_machine.transition(cid, from_, to, Actor.WORKER, {"error_class": error_class})
    assert result.state is S.WATCHING and result.redirected_from is to
    row = db.one(ctx.conn, "SELECT state, state_reason FROM contributions WHERE id = ?", (cid,))
    assert row is not None and row["state"] == "WATCHING" and row["state_reason"] == f"fix_round_failed:{error_class}"
    status = db.one(ctx.conn, "SELECT status FROM outbound_actions WHERE id = ?", (pending,))
    assert status is not None and status["status"] == "cancelled"
    assert notes and "still watching" in notes[0] and "#7" in notes[0]


@pytest.mark.parametrize(("error_class", "from_", "to"), REDIRECT_CASES)
def test_without_pr_failure_is_terminal(ctx: AppContext, error_class: str, from_: State, to: State) -> None:
    cid = make(ctx, from_)
    result = ctx.state_machine.transition(cid, from_, to, Actor.WORKER, {"error_class": error_class})
    assert result.state is to and result.redirected_from is None


def test_redirect_allowed_while_paused(ctx: AppContext) -> None:
    write_flag(ctx.paths.paused_flag, {"reason": "x"})
    cid = make(ctx, S.PACKAGING, pr_number=7)
    assert ctx.state_machine.transition(cid, S.PACKAGING, S.FAILED, Actor.GATEKEEPER).state is S.WATCHING


def test_gate_state_redirect_uses_gate_actors(ctx: AppContext) -> None:
    cid = make(ctx, S.SUBMIT_FAILED, pr_number=7)
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid, S.SUBMIT_FAILED, S.FAILED, Actor.WORKER)
    assert info.value.status == 403
    assert ctx.state_machine.transition(cid, S.SUBMIT_FAILED, S.FAILED, Actor.HUMAN).state is S.WATCHING


def test_branch_moved_disables_fixes(ctx: AppContext) -> None:
    cid = make(ctx, S.PACKAGING, pr_number=7)
    notes: list[str] = []
    ctx.state_machine.notify = notes.append
    ctx.state_machine.transition(cid, S.PACKAGING, S.FAILED, Actor.GATEKEEPER, {"error_class": "branch_moved"})
    row = db.one(ctx.conn, "SELECT watch_rounds, fixes_disabled_reason FROM contributions WHERE id = ?", (cid,))
    assert row is not None and row[0] == ctx.config.policy.limits.watch_rounds_max
    assert row["fixes_disabled_reason"] == "branch_moved"
    assert "fixes disabled" in notes[0]
    change_request(ctx, cid)
    for actor in (Actor.WORKER, Actor.HUMAN):  # M4: the owner's separate cap doesn't bypass it
        with pytest.raises(TransitionError) as info:
            ctx.state_machine.transition(cid, S.WATCHING, S.IMPLEMENTING, actor, {"reason": "x"})
        assert info.value.code == "guard_failed:fixes_disabled"
    assert state_of(ctx, cid) == "WATCHING"


def test_other_redirects_leave_fixes_enabled(ctx: AppContext) -> None:
    cid = make(ctx, S.PACKAGING, pr_number=7)
    ctx.state_machine.transition(cid, S.PACKAGING, S.FAILED, Actor.GATEKEEPER, {"error_class": "patch_mismatch"})
    row = db.one(ctx.conn, "SELECT watch_rounds, fixes_disabled_reason FROM contributions WHERE id = ?", (cid,))
    assert row is not None and tuple(row) == (0, None)


# ------------------------------------------------------------- owner fix round (ADR-0003 addendum)
OWNER_FIX = {"reason": "owner_fix_round"}


def owner_counters(ctx: AppContext, cid: str) -> tuple[int, int]:
    row = db.one(ctx.conn, "SELECT owner_fix_rounds, watch_rounds FROM contributions WHERE id = ?", (cid,))
    assert row is not None
    return int(row[0]), int(row[1])


def test_owner_fix_edge(ctx: AppContext) -> None:
    cid, action = packaged_contribution(ctx, pr_number=7)
    approval = ctx.approvals.create(action, tg_chat_id=4242, body_parts_total=1)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE contributions SET state = 'WATCHING' WHERE id = ?", (cid,))
    change_request(ctx, cid)
    result = ctx.state_machine.transition(cid, S.WATCHING, S.IMPLEMENTING, Actor.HUMAN, OWNER_FIX, "tg:4242")
    assert result.state is S.IMPLEMENTING and result.redirected_from is None
    row = db.one(ctx.conn, "SELECT state, state_reason FROM contributions WHERE id = ?", (cid,))
    assert row is not None and tuple(row) == ("IMPLEMENTING", "owner_fix_round")
    assert owner_counters(ctx, cid) == (1, 0)
    inv = db.one(ctx.conn, "SELECT status, invalidated_reason FROM approvals WHERE id = ?", (approval,))
    assert inv is not None and tuple(inv) == ("invalidated", "entered_implementing")
    event = db.one(ctx.conn, "SELECT actor, data_json FROM audit_events WHERE event = 'transition' AND cid = ?"
                   " ORDER BY seq DESC LIMIT 1", (cid,))  # fmt: skip
    assert event is not None and event["actor"] == "tg:4242"
    assert json.loads(event["data_json"])["evidence"] == OWNER_FIX


def test_owner_fix_needs_an_open_change_request(ctx: AppContext) -> None:
    cid = make(ctx, S.WATCHING, pr_number=7)
    for consumed in (None, True):
        if consumed:
            change_request(ctx, cid, consumed=True)  # an answered request doesn't count
        with pytest.raises(TransitionError) as info:
            ctx.state_machine.transition(cid, S.WATCHING, S.IMPLEMENTING, Actor.HUMAN, OWNER_FIX)
        assert info.value.status == 409 and info.value.code == "guard_failed:change_request"
    assert state_of(ctx, cid) == "WATCHING" and owner_counters(ctx, cid) == (0, 0)


def test_owner_fix_needs_a_pr(ctx: AppContext) -> None:
    cid = make(ctx, S.WATCHING)
    change_request(ctx, cid)
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid, S.WATCHING, S.IMPLEMENTING, Actor.HUMAN, OWNER_FIX)
    assert info.value.code == "guard_failed:pr_number_set"


def test_owner_fix_cap_is_separate(ctx: AppContext) -> None:
    limits = ctx.config.policy.limits
    cid = make(ctx, S.WATCHING, pr_number=7, watch_rounds=limits.watch_rounds_max)
    change_request(ctx, cid)
    ctx.state_machine.transition(cid, S.WATCHING, S.IMPLEMENTING, Actor.HUMAN, OWNER_FIX)  # watch cap spent: fine
    assert owner_counters(ctx, cid) == (1, limits.watch_rounds_max)
    capped = make(ctx, S.WATCHING, pr_number=7, owner_fix_rounds=limits.owner_fix_rounds_max)
    change_request(ctx, capped)
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(capped, S.WATCHING, S.IMPLEMENTING, Actor.HUMAN, OWNER_FIX)
    assert info.value.code == "guard_failed:owner_fix_rounds"
    worker = make(ctx, S.WATCHING, pr_number=7, owner_fix_rounds=limits.owner_fix_rounds_max)
    ctx.state_machine.transition(worker, S.WATCHING, S.IMPLEMENTING, Actor.WORKER, {"reason": "fix_round"})
    assert owner_counters(ctx, worker) == (limits.owner_fix_rounds_max, 1)  # W counts only watch_rounds
    spent = make(ctx, S.WATCHING, pr_number=7, watch_rounds=limits.watch_rounds_max)
    with pytest.raises(TransitionError) as info:  # W stays bounded by watch_rounds
        ctx.state_machine.transition(spent, S.WATCHING, S.IMPLEMENTING, Actor.WORKER, {"reason": "fix_round"})
    assert info.value.code == "guard_failed:watch_rounds"


def test_fixes_disabled_blocks_both_actors(ctx: AppContext) -> None:
    for actor, evidence in ((Actor.WORKER, {"reason": "fix_round"}), (Actor.HUMAN, OWNER_FIX)):
        cid = make(ctx, S.WATCHING, pr_number=7, fixes_disabled_reason="branch_moved")
        change_request(ctx, cid)
        with pytest.raises(TransitionError) as info:
            ctx.state_machine.transition(cid, S.WATCHING, S.IMPLEMENTING, actor, evidence)
        assert info.value.code == "guard_failed:fixes_disabled"
        assert state_of(ctx, cid) == "WATCHING" and owner_counters(ctx, cid) == (0, 0)


@pytest.mark.parametrize("stop", ["pause", "kill"])
def test_owner_fix_refused_while_paused_and_killed(ctx: AppContext, stop: str) -> None:
    cid = make(ctx, S.WATCHING, pr_number=7)
    change_request(ctx, cid)
    write_flag(ctx.paths.paused_flag if stop == "pause" else ctx.paths.killed_flag, {"reason": "x"})
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid, S.WATCHING, S.IMPLEMENTING, Actor.HUMAN, OWNER_FIX)
    assert info.value.status == 423
    assert state_of(ctx, cid) == "WATCHING" and owner_counters(ctx, cid) == (0, 0)


def test_worker_cannot_use_the_owner_reason(ctx: AppContext) -> None:
    cid = make(ctx, S.WATCHING, pr_number=7)
    for actor, to in ((Actor.WORKER, S.IMPLEMENTING), (Actor.WORKER, S.WATCHING)):
        with pytest.raises(TransitionError) as info:
            ctx.state_machine.transition(cid, S.WATCHING, to, actor, OWNER_FIX)
        assert info.value.status == 403 and info.value.code == "reserved_reason"
    assert state_of(ctx, cid) == "WATCHING" and owner_counters(ctx, cid) == (0, 0)
    forbidden = ctx.conn.execute("SELECT COUNT(*) FROM audit_events WHERE event = 'forbidden_transition' AND cid = ?",
                                 (cid,)).fetchone()  # fmt: skip
    assert forbidden[0] == 2
    other = make(ctx, S.IMPLEMENTING)
    with pytest.raises(TransitionError) as info:  # any edge, any non-human actor
        ctx.state_machine.transition(other, S.IMPLEMENTING, S.IMPLEMENTED, Actor.WORKER, OWNER_FIX)
    assert info.value.code == "reserved_reason"


def test_stop_watching_human_only(ctx: AppContext) -> None:
    for actor in (Actor.WORKER, Actor.GATEKEEPER):
        cid = make(ctx, S.WATCHING, pr_number=7)
        with pytest.raises(TransitionError) as info:
            ctx.state_machine.transition(cid, S.WATCHING, S.ABANDONED, actor)
        assert info.value.status == 403
    cid = make(ctx, S.WATCHING, pr_number=7)
    result = ctx.state_machine.transition(cid, S.WATCHING, S.ABANDONED, Actor.HUMAN)
    assert result.state is S.ABANDONED and result.redirected_from is None
    cid = make(ctx, S.WATCHING, pr_number=7)
    assert ctx.state_machine.transition(cid, S.WATCHING, S.FAILED, Actor.WORKER).state is S.WATCHING


# ----------------------------------------------------------------- terminal FYI (§14.13.1, §9.10)
def after_gate1(ctx: AppContext, state: State, issue_number: int | str = "auto", **cols: object) -> str:
    """An item whose current proposal version's Start work was consumed (it passed gate 1)."""
    cols.setdefault("proposal_version", 1)
    cid = insert_contribution(ctx, state, upstream="runatlantis/atlantis", issue_number=issue_number, **cols)
    consumed_proposal(ctx, cid)
    return cid


def terminal_rows(ctx: AppContext) -> list[sqlite3.Row]:
    return ctx.conn.execute(
        "SELECT dedupe_key, priority, thread_key, payload_json FROM tg_outbox WHERE dedupe_key LIKE 'terminal:%'"
    ).fetchall()


def test_planning_abandoned_after_gate_1_queues_one_fyi(ctx: AppContext) -> None:
    cid = after_gate1(ctx, S.PLANNING, issue_number=2607)
    evidence = {"reason": "plan_infeasible", "summary": "already fixed on main"}
    ctx.state_machine.transition(cid, S.PLANNING, S.ABANDONED, Actor.WORKER, evidence)
    rows = terminal_rows(ctx)
    assert len(rows) == 1
    row = rows[0]
    assert (row["dedupe_key"], row["priority"], row["thread_key"]) == (f"terminal:{cid}", 2, "feed")
    payload = row["payload_json"]
    assert f"<code>{cid}</code>" in payload and "already fixed on main" in payload
    assert "atlantis#2607" in payload and "plan_infeasible" in payload and "Stage: planning" in payload


@pytest.mark.parametrize(
    ("from_", "actor"),
    [(S.FETCHING, Actor.WORKER), (S.PLANNING, Actor.WORKER), (S.IMPLEMENTING, Actor.WORKER),
     (S.VALIDATING, Actor.WORKER), (S.PROPOSAL_APPROVED, Actor.WORKER), (S.SUBMIT_FAILED, Actor.GATEKEEPER)],
)  # fmt: skip
def test_failed_after_gate_1_queues_one_fyi(ctx: AppContext, from_: State, actor: Actor) -> None:
    cid = after_gate1(ctx, from_)
    ctx.state_machine.transition(cid, from_, S.FAILED, actor, {"error_class": "output_invalid"})
    rows = terminal_rows(ctx)
    assert [r["dedupe_key"] for r in rows] == [f"terminal:{cid}"]
    assert "Failed" in rows[0]["payload_json"] and "output_invalid" in rows[0]["payload_json"]


def test_no_fyi_before_gate_1_on_redirect_or_by_the_owner(ctx: AppContext) -> None:
    for from_ in (S.VETTING, S.SCOUTING):  # before gate 1: no proposal at all
        cid = insert_contribution(ctx, from_)
        ctx.state_machine.transition(cid, from_, S.FAILED, Actor.WORKER, {"error_class": "x"})
    rescout = insert_contribution(ctx, S.SCOUTING, proposal_version=1)  # a version, but no Start work
    ctx.state_machine.transition(rescout, S.SCOUTING, S.FAILED, Actor.WORKER, {"error_class": "x"})
    live = after_gate1(ctx, S.PACKAGING, pr_number=7)  # live-PR redirect: WATCHING keeps its own note
    assert ctx.state_machine.transition(live, S.PACKAGING, S.FAILED, Actor.GATEKEEPER).state is S.WATCHING
    dropped = after_gate1(ctx, S.RESCOPE_REQUIRED)  # the owner's own Drop
    ctx.state_machine.transition(dropped, S.RESCOPE_REQUIRED, S.ABANDONED, Actor.HUMAN)
    stopped = after_gate1(ctx, S.WATCHING, pr_number=8)  # the owner's own 🛑 Stop watching
    ctx.state_machine.transition(stopped, S.WATCHING, S.ABANDONED, Actor.HUMAN)
    assert terminal_rows(ctx) == []


def test_terminal_fyi_is_deduplicated_per_cid(ctx: AppContext) -> None:
    cid = after_gate1(ctx, S.PLANNING)
    ctx.state_machine.transition(cid, S.PLANNING, S.ABANDONED, Actor.WORKER, {"reason": "plan_infeasible"})
    row = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
    assert row is not None
    assert terminal_fyi.queue_terminal_fyi(ctx, row, S.PLANNING, S.ABANDONED, "plan_infeasible", {}) is None
    assert len(terminal_rows(ctx)) == 1


def test_a_failing_fyi_never_blocks_the_transition(ctx: AppContext, caplog: pytest.LogCaptureFixture) -> None:
    cid = after_gate1(ctx, S.IMPLEMENTING)

    def broken(*args: object) -> None:
        ctx.conn.execute("UPDATE contributions SET notes = 'half' WHERE id = ?", (cid,))  # rolled back alone
        raise RuntimeError("render bug")

    ctx.state_machine.on_terminal = broken
    with caplog.at_level(logging.ERROR):
        assert ctx.state_machine.transition(cid, S.IMPLEMENTING, S.FAILED, Actor.WORKER).state is S.FAILED
    row = db.one(ctx.conn, "SELECT state, notes FROM contributions WHERE id = ?", (cid,))
    assert row is not None and tuple(row) == ("FAILED", None)
    assert [getattr(r, "cid", None) for r in caplog.records if getattr(r, "event", None) == "terminal_fyi_failed"] \
        == [cid]  # fmt: skip


def test_terminal_fyi_without_telegram_is_dropped(ctx: AppContext) -> None:
    profile = ctx.paths.config_dir / "profile.yaml"
    profile.write_text(profile.read_text().replace("user_id: 4242", "user_id: 0"))
    ctx.snapshots.apply_reload(ctx.snapshots.prepare_reload().hash, "uid:1000/operator")
    cid = after_gate1(ctx, S.PLANNING)
    ctx.state_machine.transition(cid, S.PLANNING, S.ABANDONED, Actor.WORKER, {"reason": "plan_infeasible"})
    assert state_of(ctx, cid) == "ABANDONED" and terminal_rows(ctx) == []


# ------------------------------------------------------------------------- expiry
def test_approval_expiry_keeps_awaiting_then_sweep_expires(ctx: AppContext, clock: FakeClock) -> None:
    cid, action = packaged_contribution(ctx)
    ctx.approvals.create(action, tg_chat_id=4242, body_parts_total=1)
    clock.advance(hours=73)
    ctx.approvals.expire_due()
    assert state_of(ctx, cid) == "AWAITING_APPROVAL"
    assert ctx.state_machine.sweep_expired() == []
    clock.advance(days=5)
    assert ctx.state_machine.sweep_expired() == [cid]
    assert state_of(ctx, cid) == "EXPIRED"


def test_sweep_with_pr_goes_to_watching_and_cancels_push_update(ctx: AppContext, clock: FakeClock) -> None:
    cid, action = packaged_contribution(ctx, pr_number=7)
    ctx.approvals.create(action, tg_chat_id=4242, body_parts_total=1)
    clock.advance(days=8)
    ctx.approvals.expire_due()
    assert ctx.state_machine.sweep_expired() == [cid]
    assert state_of(ctx, cid) == "WATCHING"
    row = db.one(ctx.conn, "SELECT status FROM outbound_actions WHERE id = ?", (action,))
    assert row is not None and row["status"] == "cancelled"
