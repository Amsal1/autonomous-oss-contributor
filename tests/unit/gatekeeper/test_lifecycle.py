"""CR-001 graceful lifecycle in the gatekeeper: drain / down / up, distinct from kill."""

from __future__ import annotations

import json

import pytest

from ossbot.common.clock import from_iso
from ossbot.common.control_flags import read_lifecycle, require_up
from ossbot.common.schemas import State
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.control import ControlConflict
from tests.conftest import UID_OPERATOR, UID_WORKER, insert_contribution, packaged_contribution, write_flag
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


def test_up_reports_the_real_resumed_count(ctx: AppContext) -> None:
    """CR-001: "N resumed" = non-terminal items whose latest stage run was interrupted or left running."""
    sent: list[str] = []
    ctx.control.notify = sent.append

    def item(state: State, *statuses: str) -> None:
        cid = insert_contribution(ctx, state)
        for n, st in enumerate(statuses, start=1):
            ctx.conn.execute("INSERT INTO stage_runs (id, cid, stage, attempt, status, started_at) VALUES"
                             " (?, ?, 'plan', ?, ?, '2026-10-18T00:00:00Z')", (f"r_{cid}_{n}", cid, n, st))  # fmt: skip

    item(State.PLANNING, "interrupted")  # drained mid-stage
    item(State.IMPLEMENTING, "succeeded", "running")  # the runner died
    item(State.PLANNED, "interrupted", "succeeded")  # already resumed
    item(State.FAILED, "interrupted")  # terminal
    ctx.control.drain("uid:0/operator", "down", 60)
    assert ctx.control.requeued() == 2
    ctx.control.up("uid:0/operator")
    assert any("Back up: 2 resumed" in s for s in sent)


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
    st = ctx.control.lifecycle_status(egress_default="open", tier_default="self_improve")
    assert st["lifecycle"] == "up" and st["awaiting_approval"] == 1
    assert st["executing_actions"] == 0 and st["running_stages"] == 0
    assert st["egress_mode"] == "open" and st["hermes_tier"] == "self_improve"  # no flag: the defaults


def test_lifecycle_status_shows_the_flags_fail_closed(ctx: AppContext) -> None:
    """PLAN item 39: status.ps1 shows the egress mode and Hermes tier in effect, read like every reader."""
    write_flag(ctx.paths.control_dir / "egress-mode", {"mode": "research", "actor": "x", "ts": "t"})
    write_flag(ctx.paths.control_dir / "hermes-tier", {"tier": "locked", "actor": "x", "ts": "t"})
    st = ctx.control.lifecycle_status(egress_default="open", tier_default="self_improve")
    assert (st["egress_mode"], st["hermes_tier"]) == ("research", "locked")
    (ctx.paths.control_dir / "egress-mode").write_text("{garbage")
    (ctx.paths.control_dir / "hermes-tier").write_text('{"tier": "root"}')
    st = ctx.control.lifecycle_status(egress_default="open", tier_default="self_improve")
    assert (st["egress_mode"], st["hermes_tier"]) == ("allowlist", "locked")


def test_lifecycle_request_file_follows_the_root_helper_rules(ctx: AppContext) -> None:
    """PLAN item 42: what ossbot-lifecycle --request accepts: a regular 0640 file (gatekeeper or root),
    at most 4 KiB, with a fresh ts, scope workers and action down|up; the request is audited."""
    out = ctx.control.request_lifecycle("down", "tg:4242", 120)
    assert out == {"requested": True, "action": "down", "scope": "workers", "timeout_s": 120}
    path = ctx.paths.lifecycle_request
    assert path.is_file() and not path.is_symlink() and (path.stat().st_mode & 0o777) == 0o640
    raw = path.read_bytes()
    assert len(raw) <= 4096
    doc = json.loads(raw)
    assert set(doc) == {"action", "scope", "actor", "ts", "timeout_s"}
    assert (doc["action"], doc["scope"], doc["timeout_s"]) == ("down", "workers", 120)
    assert abs((from_iso(doc["ts"]) - ctx.clock.now()).total_seconds()) < 1
    event = ctx.conn.execute("SELECT actor, data_json FROM audit_events WHERE event = 'lifecycle_requested'").fetchone()
    assert event["actor"] == "tg:4242" and json.loads(event["data_json"])["action"] == "down"
    assert ctx.control.view().lifecycle == "up"  # nothing changes until root runs ossctl down


def test_lifecycle_request_up_refused_while_killed_and_no_distro_scope(ctx: AppContext) -> None:
    ctx.control.kill("test", "tg:4242")
    with pytest.raises(ControlConflict):
        ctx.control.request_lifecycle("up", "tg:4242", 120)
    assert not ctx.paths.lifecycle_request.exists()
    ctx.control.request_lifecycle("down", "tg:4242", 120)  # a down while killed is harmless
    assert ctx.paths.lifecycle_request.exists()
    with pytest.raises(ControlConflict):
        ctx.control.request_lifecycle("down", "op", 120, scope="distro")


async def test_lifecycle_request_api(ctx: AppContext) -> None:
    async with client(ctx, UID_WORKER) as c:
        assert (await c.post("/v1/control/lifecycle-request", json={"action": "down"})).status_code == 403
    async with client(ctx, UID_OPERATOR) as c:
        assert (await c.post("/v1/control/lifecycle-request", json={"action": "reboot"})).status_code == 400
        assert (await c.post("/v1/control/lifecycle-request",
                             json={"action": "down", "scope": "distro"})).status_code == 400  # fmt: skip
        resp = await c.post("/v1/control/lifecycle-request", json={"action": "down"})
        assert resp.status_code == 202
        assert resp.json()["timeout_s"] == ctx.config.policy.lifecycle.drain_timeout_s
        write_flag(ctx.paths.killed_flag, {"level": "soft", "reason": "x", "actor": "root"})
        assert (await c.post("/v1/control/lifecycle-request", json={"action": "up"})).status_code == 409


async def test_lifecycle_api_operator_only(ctx: AppContext) -> None:
    async with client(ctx, UID_WORKER) as c:
        assert (await c.post("/v1/control/drain", json={"timeout_s": 5})).status_code == 403
    async with client(ctx, UID_OPERATOR) as c:
        drained = await c.post("/v1/control/drain", json={"timeout_s": 5, "scope": "workers"})
        assert drained.status_code == 200 and drained.json()["lifecycle"] == "draining"
        lc = (await c.get("/v1/control/lifecycle")).json()
        assert lc["lifecycle"] == "draining" and lc["scope"] == "workers" and lc["control"]["state"] == "running"
        assert set(lc["telegram"]) >= {"last_ok_at", "reconnects", "consecutive_failures"}
        # every CR-001 field status.ps1 prints (PLAN item 39)
        assert set(lc) >= {"lifecycle", "scope", "lifecycle_since", "running_stages", "executing_actions",
                           "awaiting_approval", "outbox_pending", "egress_mode", "hermes_tier"}  # fmt: skip
        assert set(lc["control"]) >= {"state", "dry_run", "kill_level", "reason", "lifecycle"}
        assert (await c.post("/v1/control/down", json={"finished": 1, "interrupted": 0})).json()["lifecycle"] == "down"
        write_flag(ctx.paths.killed_flag, {"level": "soft", "reason": "x", "actor": "root"})
        refused = await c.post("/v1/control/up", json={})
        assert refused.status_code == 409
        ctx.paths.killed_flag.unlink()  # resumed by root before the gatekeeper noticed the kill
        assert (await c.post("/v1/control/up", json={})).json()["lifecycle"] == "up"
