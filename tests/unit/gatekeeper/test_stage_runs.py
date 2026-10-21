"""Stage runs (DESIGN §9.3): open / heartbeat / close, one running stage per contribution, the
re-queue limit, and refusal past the control state for every runner stage (AC 10, CR-001)."""

from __future__ import annotations

import pytest

from ossbot.common.schemas import STAGE_ENTRYPOINTS, StageRunOpen, StageRunUpdate, State
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.stage_runs import StageRunRefused
from tests.conftest import UID_OPERATOR, UID_WORKER, insert_contribution
from tests.fakes.holds import hold_request
from tests.unit.gatekeeper.api_helpers import client

W = "uid:2001/worker"


def test_open_heartbeat_close(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.VETTED)
    run = ctx.stage_runs.open(cid, StageRunOpen(stage="scout", profile="research", model="m", effort="high"), W)
    assert run["attempt"] == 1
    ctx.stage_runs.update(run["id"], StageRunUpdate(), W)  # heartbeat
    ctx.stage_runs.update(run["id"], StageRunUpdate(status="succeeded", model_actual="m"), W)
    rows = ctx.stage_runs.list(None, cid)
    assert rows[0]["status"] == "succeeded" and rows[0]["model_actual"] == "m" and rows[0]["finished_at"]
    with pytest.raises(StageRunRefused) as info:
        ctx.stage_runs.update(run["id"], StageRunUpdate(), W)
    assert info.value.code == "stage_run_final"


def test_one_running_stage_per_contribution(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.VETTED)
    ctx.stage_runs.open(cid, StageRunOpen(stage="scout"), W)
    with pytest.raises(StageRunRefused) as info:
        ctx.stage_runs.open(cid, StageRunOpen(stage="scout"), W)
    assert info.value.code == "stage_running"


def test_requeue_limit_after_three_interrupts(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.FETCHING)
    for attempt in (1, 2, 3):
        run = ctx.stage_runs.open(cid, StageRunOpen(stage="fetch"), W)
        assert run["attempt"] == attempt
        ctx.stage_runs.update(run["id"], StageRunUpdate(status="interrupted", error_class="interrupted"), W)
    with pytest.raises(StageRunRefused) as info:
        ctx.stage_runs.open(cid, StageRunOpen(stage="fetch"), W)
    assert info.value.code == "requeue_limit"


def test_unknown_stage_refused(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.VETTED)
    with pytest.raises(StageRunRefused) as info:
        ctx.stage_runs.open(cid, StageRunOpen(stage="deploy"), W)
    assert info.value.code == "unknown_stage"


@pytest.mark.parametrize("stage", STAGE_ENTRYPOINTS)
@pytest.mark.parametrize("how", ["paused", "killed", "draining"])
def test_no_stage_opens_past_the_control_state(ctx: AppContext, stage: str, how: str) -> None:
    """Every runner stage incl. scout (STAGE_ENTRYPOINTS, shared with runner.pipeline)."""
    cid = insert_contribution(ctx, State.VETTED)
    if how == "paused":
        ctx.control.pause("test", "uid:1000/operator")
    elif how == "killed":
        ctx.control.kill("test", "uid:1000/operator")
    else:
        ctx.control.drain("uid:0/operator", "ossctl down", timeout_s=60, scope="distro")
    with pytest.raises(StageRunRefused) as info:
        ctx.stage_runs.open(cid, StageRunOpen(stage=stage), W)
    assert info.value.status == 423
    assert ctx.stage_runs.list(None, cid) == []


@pytest.mark.parametrize("stage", STAGE_ENTRYPOINTS)
async def test_no_stage_opens_while_held(ctx: AppContext, stage: str) -> None:
    """Open-discovery §13.3: an egress-anomaly hold stops every stage of that contribution (423)."""
    cid = insert_contribution(ctx, State.VALIDATING)
    ctx.holds.hold(cid, hold_request(), "uid:2002/sandbox")
    with pytest.raises(StageRunRefused) as info:
        ctx.stage_runs.open(cid, StageRunOpen(stage=stage), W)
    assert (info.value.status, info.value.code) == (423, "contribution_held")
    async with client(ctx, UID_WORKER) as c:
        resp = await c.post(f"/v1/contributions/{cid}/stage-runs", json={"stage": stage})
    assert (resp.status_code, resp.json()["error"]) == (423, "contribution_held")
    assert ctx.stage_runs.list(None, cid) == []


async def test_api_roles(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.VETTED)
    async with client(ctx, UID_OPERATOR) as c:
        assert (await c.post(f"/v1/contributions/{cid}/stage-runs", json={"stage": "vet"})).status_code == 403
    async with client(ctx, UID_WORKER) as c:
        resp = await c.post(f"/v1/contributions/{cid}/stage-runs", json={"stage": "vet"})
        assert resp.status_code == 201
        run_id = resp.json()["id"]
        assert (await c.post(f"/v1/stage-runs/{run_id}", json={"status": "failed"})).status_code == 200
        assert (await c.get("/v1/stage-runs?status=failed")).json()[0]["id"] == run_id
