"""Per-contribution holds (open-discovery design §13.3, ADR-0023): idempotent, only this cid's approvals
invalidated, StageRuns.open and /package refused with 423 ``contribution_held``, released only by the
human, the card published once after the commit, a raising publisher keeps the hold."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from pydantic import ValidationError

from ossbot.common.schemas import HoldRequest, HoldView, StageRunOpen, State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.holds import HoldRefused
from ossbot.gatekeeper.stage_runs import StageRunRefused
from tests.conftest import UID_WORKER, insert_contribution, packaged_contribution
from tests.fakes.holds import hold_body, hold_request
from tests.githarness import GitWorld, package_request, stage_artifacts
from tests.unit.gatekeeper.api_helpers import client
from tests.unit.gatekeeper.test_package_verify import packaging

SANDBOX = "uid:2002/sandbox"
W = "uid:2001/worker"


def _approval(ctx: AppContext, action_id: str) -> str:
    with db.transaction(ctx.conn):
        return ctx.create_approval(action_id, "test")


def _status(ctx: AppContext, approval_id: str) -> tuple[str, str | None]:
    row = db.one(ctx.conn, "SELECT status, invalidated_reason FROM approvals WHERE id = ?", (approval_id,))
    assert row is not None
    return str(row["status"]), row["invalidated_reason"]


def _events(ctx: AppContext, event: str) -> list[dict[str, Any]]:
    rows = ctx.conn.execute("SELECT data_json FROM audit_events WHERE event = ? ORDER BY seq", (event,))
    return [json.loads(r[0]) for r in rows]


def test_hold_invalidates_only_this_contributions_approvals(ctx: AppContext) -> None:
    cid, action = packaged_contribution(ctx)
    other, other_action = packaged_contribution(ctx)
    mine, theirs = _approval(ctx, action), _approval(ctx, other_action)
    found, created = ctx.holds.hold(cid, hold_request(), SANDBOX)
    assert created and found.cid == cid and found.released_at is None
    assert _status(ctx, mine) == ("invalidated", "egress_anomaly")
    assert _status(ctx, theirs) == ("pending", None)
    assert ctx.holds.is_held(cid) and not ctx.holds.is_held(other)
    assert db.one(ctx.conn, "SELECT state FROM contributions WHERE id = ?", (cid,))[0] == "AWAITING_APPROVAL"  # I13
    held = _events(ctx, "contribution_held")
    assert held[0]["hold"] == found.id and held[0]["invalidated"] == [mine] and held[0]["up_bytes"] == 312 * 1024**2


def test_repeat_is_idempotent_and_audited(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.VALIDATING)
    first, created = ctx.holds.hold(cid, hold_request(), SANDBOX)
    again, created_again = ctx.holds.hold(cid, hold_request(run_id="validate-other"), W)
    assert created and not created_again and again == first
    assert len(ctx.holds.open_holds()) == 1
    assert _events(ctx, "contribution_hold_repeat") == [{"hold": first.id, "run": "validate-other"}]


def test_unknown_cid_refused(ctx: AppContext) -> None:
    with pytest.raises(HoldRefused) as info:
        ctx.holds.hold("c_0000000000", hold_request(), SANDBOX)
    assert (info.value.status, info.value.code) == (404, "unknown_cid")


def test_release_reopens_the_stage_and_a_new_hold_is_possible(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.VALIDATING)
    ctx.holds.hold(cid, hold_request(), SANDBOX)
    with pytest.raises(StageRunRefused) as info:
        ctx.stage_runs.open(cid, StageRunOpen(stage="validate"), W)
    assert (info.value.status, info.value.code) == (423, "contribution_held")
    assert ctx.stage_runs.list(None, cid) == []
    released = ctx.holds.release(cid, "tg:4242")
    assert released.released_by == "tg:4242" and released.released_at is not None
    assert not ctx.holds.is_held(cid)
    assert ctx.stage_runs.open(cid, StageRunOpen(stage="validate"), W)["attempt"] == 1
    with pytest.raises(HoldRefused) as again:
        ctx.holds.release(cid, "tg:4242")
    assert (again.value.status, again.value.code) == (404, "no_open_hold")
    _, created = ctx.holds.hold(cid, hold_request(), SANDBOX)
    assert created and len(ctx.holds.open_holds(include_released=True)) == 2


def test_other_contributions_keep_running(ctx: AppContext) -> None:
    held = insert_contribution(ctx, State.VALIDATING)
    free = insert_contribution(ctx, State.VALIDATING)
    ctx.holds.hold(held, hold_request(), SANDBOX)
    assert ctx.stage_runs.open(free, StageRunOpen(stage="validate"), W)["attempt"] == 1


def test_active_list_carries_held(ctx: AppContext) -> None:
    held = insert_contribution(ctx, State.VALIDATING)
    free = insert_contribution(ctx, State.VALIDATING)
    ctx.holds.hold(held, hold_request(), SANDBOX)
    rows = {r["id"]: r["held"] for r in ctx.contributions.list(None, 50, active=True)}
    assert rows[held] is True and rows[free] is False


def test_publisher_called_once_after_the_commit(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.VALIDATING)
    seen: list[tuple[HoldView, bool]] = []

    def publish(view: HoldView) -> None:
        seen.append((view, ctx.conn.in_transaction))
        assert ctx.holds.is_held(view.cid)  # the row is committed and visible

    ctx.holds.publish_hold = publish
    ctx.holds.hold(cid, hold_request(), SANDBOX)
    ctx.holds.hold(cid, hold_request(), SANDBOX)  # the repeat publishes nothing
    assert len(seen) == 1 and seen[0][1] is False and seen[0][0].detail.top_hosts[0] == "pypi.org:443"


def test_raising_publisher_keeps_the_hold(ctx: AppContext, caplog: pytest.LogCaptureFixture) -> None:
    cid = insert_contribution(ctx, State.VALIDATING)

    def boom(view: HoldView) -> None:
        raise RuntimeError("telegram down")

    ctx.holds.publish_hold = boom
    with caplog.at_level(logging.ERROR):
        _, created = ctx.holds.hold(cid, hold_request(), SANDBOX)
    assert created and ctx.holds.is_held(cid)
    assert any(getattr(r, "event", "") == "hold_card_failed" for r in caplog.records)


@pytest.mark.parametrize("host", ["Pypi.org", "a b", "evil.com:999999", "x" * 254, "<b>x</b>", "ex\u00e4mple.com", ""])
def test_schema_refuses_bad_host_strings(host: str) -> None:
    with pytest.raises(ValidationError):
        HoldRequest.model_validate(hold_body(top_hosts=[host]))


@pytest.mark.parametrize("overrides", [
    {"top_hosts": [f"h{i}.example.com" for i in range(11)]}, {"breached": []}, {"stage": "research"},
    {"up_bytes": -1}, {"reason": "other"}, {"extra": 1}, {"run_id": "Bad Run"}, {"egress_mode": "wide"},
])  # fmt: skip
def test_schema_refuses_bad_bodies(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        HoldRequest.model_validate(hold_body(**overrides))


def test_schema_accepts_an_implement_hold(ctx: AppContext) -> None:
    """ADR-0024: a networked coder run is metered as stage implement; its hold is recorded like the others."""
    req = HoldRequest.model_validate(hold_body(stage="implement", run_id="impl-abc123"))
    cid = insert_contribution(ctx, State.IMPLEMENTING)
    view, created = ctx.holds.hold(cid, req, SANDBOX)
    assert created and view.detail.stage == "implement" and ctx.holds.is_held(cid)


def test_schema_accepts_the_unknown_host_marker() -> None:
    assert HoldRequest.model_validate(hold_body(top_hosts=["?", "pypi.org"])).top_hosts == ["?", "pypi.org"]


async def test_package_refused_while_held(gctx: AppContext, world: GitWorld) -> None:
    out = world.package(world.base, {"src/app.py": "print('held')\n"})
    cid = packaging(gctx)
    sums = stage_artifacts(gctx.paths, cid, 1, out.out)
    gctx.holds.hold(cid, hold_request(), SANDBOX)
    async with client(gctx, UID_WORKER) as c:
        resp = await c.post(f"/v1/contributions/{cid}/package",
                            json=package_request(gctx.paths, cid, 1, sums, out.head, world.base))  # fmt: skip
    assert resp.status_code == 423
    assert resp.json()["error"] == "contribution_held"
    assert db.one(gctx.conn, "SELECT COUNT(*) FROM outbound_actions WHERE cid = ?", (cid,))[0] == 0
