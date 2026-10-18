"""Track record (DESIGN §9.14): proposal -> PR -> merge per repo, kind and size class."""

from __future__ import annotations

from ossbot.common.schemas import State
from ossbot.gatekeeper import db, outcomes
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.proposals import ProposalService
from ossbot.gatekeeper.state_machine import Actor
from tests.conftest import insert_contribution
from tests.unit.gatekeeper.test_proposals import OWNER, scouting, start, submit

S = State


def outcome(ctx: AppContext, cid: str) -> dict[str, object]:
    row = db.one(ctx.conn, "SELECT * FROM outcomes WHERE cid = ?", (cid,))
    assert row is not None
    return dict(row)


def test_proposed_started_and_finished(ctx: AppContext) -> None:
    cid = scouting(ctx)
    out = submit(ctx, cid)
    first = outcome(ctx, cid)
    assert first["proposed_at"] and first["kind"] == "bug" and first["size_class"] == "s" and first["result"] is None
    start(ctx, out["approval_id"])
    assert outcome(ctx, cid)["started_at"]
    ctx.state_machine.transition(cid, S.PROPOSAL_APPROVED, S.FAILED, Actor.WORKER, {"error_class": "x"})
    assert outcome(ctx, cid)["result"] == "failed"


def test_decline_is_recorded_and_undo_clears_it(ctx: AppContext) -> None:
    cid = scouting(ctx)
    out = submit(ctx, cid)
    ProposalService(ctx).decline(out["approval_id"], "skipped", OWNER)
    assert outcome(ctx, cid)["result"] == "declined"
    ProposalService(ctx).undo(cid, out["approval_id"], OWNER)
    assert outcome(ctx, cid)["result"] is None


def test_imports_and_pre_gate_rows_are_not_tracked(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, S.IMPORTED, source="operator", issue_number=None)
    ctx.state_machine.transition(cid, S.IMPORTED, S.FETCHING, Actor.WORKER)
    assert db.one(ctx.conn, "SELECT 1 FROM outcomes WHERE cid = ?", (cid,)) is None


def test_merge_rate_by_class(ctx: AppContext) -> None:
    a = scouting(ctx)
    submit(ctx, a)
    b = scouting(ctx, source="user")
    submit(ctx, b, estimate={"files": 6, "code_lines": 200, "test_lines": 10, "class": "m"})
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE outcomes SET result = 'merged' WHERE cid = ?", (a,))
    rates = outcomes.merge_rate_by_class(ctx.conn)
    assert rates["s"] == {"proposed": 1, "merged": 1} and rates["m"] == {"proposed": 1}
