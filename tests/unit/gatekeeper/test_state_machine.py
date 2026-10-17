"""State machine: actor table, generated FAILED edges, guards, counters, live-PR redirect (§9.2, §22.1)."""

from __future__ import annotations

import json

import pytest

from ossbot.common.clock import FakeClock
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.state_machine import (
    ALLOWED,
    APPROVAL_STATES,
    GATE,
    WORKING,
    Actor,
    TransitionError,
    all_edges,
)
from tests.conftest import insert_action, insert_contribution, packaged_contribution, write_flag

S = State
EDGES = all_edges()


def guard_columns(from_: State, to: State) -> dict[str, object]:
    """Row values that satisfy the guards of an edge (so the actor check is what is tested)."""
    cols: dict[str, object] = {}
    if to is S.WATCHING and from_ in {S.AWAITING_APPROVAL, S.SUBMIT_FAILED, S.SUBMITTING}:
        cols["pr_number"] = 7
    if from_ is S.FETCHED and to is S.IMPLEMENTED:
        cols["source"] = "operator"
    return cols


def make(ctx: AppContext, state: State, **cols: object) -> str:
    source = str(cols.pop("source", "discover"))
    cid = insert_contribution(ctx, state, source=source, **cols)
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
def test_claim_guard_blocks_until_claim_done(ctx: AppContext) -> None:
    cid = make(ctx, S.VETTED, vetting_json=json.dumps({"requires_claim_comment": True}))
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid, S.VETTED, S.FETCHING, Actor.WORKER)
    assert info.value.code == "guard_failed:claim"
    # dry-run (default ON): a dry_run_done claim satisfies the guard
    insert_action(ctx, cid, "issue_claim", {"target_number": 101, "body": "x"}, status="dry_run_done")
    ctx.state_machine.transition(cid, S.VETTED, S.FETCHING, Actor.WORKER)


def test_claim_guard_live_needs_done(ctx: AppContext) -> None:
    ctx.control.set_dry_run(False, "tg:4242")
    cid = make(ctx, S.VETTED, vetting_json=json.dumps({"requires_claim_comment": True}))
    insert_action(ctx, cid, "issue_claim", {"target_number": 101, "body": "x"}, status="dry_run_done")
    with pytest.raises(TransitionError):
        ctx.state_machine.transition(cid, S.VETTED, S.FETCHING, Actor.WORKER)
    insert_action(ctx, cid, "issue_claim", {"target_number": 101, "body": "x"}, status="done")
    ctx.state_machine.transition(cid, S.VETTED, S.FETCHING, Actor.WORKER)


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
    row = db.one(ctx.conn, "SELECT watch_rounds FROM contributions WHERE id = ?", (cid,))
    assert row is not None and row[0] == ctx.config.policy.limits.watch_rounds_max
    assert "fixes disabled" in notes[0]
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid, S.WATCHING, S.IMPLEMENTING, Actor.WORKER)
    assert info.value.code == "guard_failed:watch_rounds"


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
