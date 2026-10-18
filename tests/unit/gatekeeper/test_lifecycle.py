"""CR-001 graceful lifecycle in the gatekeeper: drain / down / up, distinct from kill."""

from __future__ import annotations

import pytest

from ossbot.common.control_flags import read_lifecycle, require_up
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.control import ControlConflict
from tests.conftest import UID_OPERATOR, UID_WORKER, packaged_contribution, write_flag
from tests.unit.gatekeeper.api_helpers import client
from tests.unit.gatekeeper.test_approvals import card, status, tap


def test_drain_down_up_cycle_keeps_approvals(ctx: AppContext) -> None:
    sent: list[str] = []
    ctx.control.notify = sent.append
    _, action = packaged_contribution(ctx)
    pending, _ = card(ctx, action, message_id=1)
    _, action2 = packaged_contribution(ctx)
    armed, t2 = card(ctx, action2, message_id=2)
    tap(ctx, t2, message_id=2)

    view = ctx.control.drain("uid:0/operator", "ossctl down (graceful)", 120)
    assert view.lifecycle == "draining" and view.state == "running"  # not a pause or kill
    info = read_lifecycle(ctx.paths.control_dir)
    assert info is not None and info.scope == "distro" and info.timeout_s == 120
    with pytest.raises(RuntimeError):
        require_up(ctx.paths.control_dir, "stage:fetch")

    assert ctx.control.down("uid:0/operator", finished=2, interrupted=1).lifecycle == "down"
    assert any("Going down: 2 finished, 1 interrupted" in s for s in sent)
    assert status(ctx, pending) == "pending" and status(ctx, armed) == "armed"  # not invalidated

    assert ctx.control.up("uid:0/operator").lifecycle == "up"
    assert not ctx.paths.lifecycle_flag.exists()
    assert any("Back up: 0 resumed, 2 awaiting approval" in s for s in sent)
    events = [r["event"] for r in ctx.conn.execute("SELECT event FROM audit_events ORDER BY seq")]
    assert events.index("lifecycle_draining") < events.index("lifecycle_down") < events.index("lifecycle_up")


def test_kill_overrides_lifecycle_and_up_is_refused(ctx: AppContext) -> None:
    ctx.control.drain("op", "down", 60)
    ctx.control.kill("test", "tg:4242")
    view = ctx.control.view()
    assert view.state == "killed" and view.lifecycle == "draining"
    with pytest.raises(ControlConflict):
        ctx.control.up("op")
    assert ctx.paths.lifecycle_flag.exists()


def test_unparseable_lifecycle_flag_is_down(ctx: AppContext) -> None:
    ctx.paths.lifecycle_flag.write_text("{not json")
    info = read_lifecycle(ctx.paths.control_dir)
    assert info is not None and info.state == "down" and info.scope == "workers"


def test_lifecycle_status_counts(ctx: AppContext) -> None:
    _, action = packaged_contribution(ctx)
    card(ctx, action)
    st = ctx.control.lifecycle_status()
    assert st["lifecycle"] == "up" and st["awaiting_approval"] == 1
    assert st["executing_actions"] == 0 and st["running_stages"] == 0


async def test_lifecycle_api_operator_only(ctx: AppContext) -> None:
    async with client(ctx, UID_WORKER) as c:
        assert (await c.post("/v1/control/drain", json={"timeout_s": 5})).status_code == 403
    async with client(ctx, UID_OPERATOR) as c:
        drained = await c.post("/v1/control/drain", json={"timeout_s": 5, "scope": "workers"})
        assert drained.status_code == 200 and drained.json()["lifecycle"] == "draining"
        lc = (await c.get("/v1/control/lifecycle")).json()
        assert lc["lifecycle"] == "draining" and lc["scope"] == "workers" and lc["control"]["state"] == "running"
        assert set(lc["telegram"]) >= {"last_ok_at", "reconnects", "consecutive_failures"}
        assert (await c.post("/v1/control/down", json={"finished": 1, "interrupted": 0})).json()["lifecycle"] == "down"
        write_flag(ctx.paths.killed_flag, {"level": "soft", "reason": "x", "actor": "root"})
        refused = await c.post("/v1/control/up", json={})
        assert refused.status_code == 409
        ctx.paths.killed_flag.unlink()  # resumed by root before the gatekeeper noticed the kill
        assert (await c.post("/v1/control/up", json={})).json()["lifecycle"] == "up"
