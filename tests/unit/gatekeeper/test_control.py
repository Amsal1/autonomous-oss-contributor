"""Kill/pause blocks every stage, job, filter op, outbound kind, checkpoint and working transition (AC 10).

The entrypoint lists are the shared constants in ``ossbot.common.schemas``; the runner, sandboxd
and the Docker filter call the same ``require_running`` so later FEATs extend this parametrization.
"""

from __future__ import annotations

import json

import pytest

from ossbot.common.control_flags import Blocked, require_running
from ossbot.common.schemas import (
    DOCKER_FILTER_OPERATIONS,
    OUTBOUND_KINDS,
    SANDBOX_JOB_ENTRYPOINTS,
    STAGE_ENTRYPOINTS,
    SUBMISSION_CHECKPOINTS,
    State,
)
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.control import ModeChanged
from ossbot.gatekeeper.state_machine import ALLOWED, BLOCKED_WHILE_STOPPED, Actor, TransitionError
from tests.conftest import UID_OPERATOR, UID_REQUESTER, UID_SANDBOX, UID_WORKER
from tests.unit.gatekeeper.api_helpers import client
from tests.unit.gatekeeper.test_authz import outbox_texts
from tests.unit.gatekeeper.test_state_machine import guard_columns, make

STOPPED = ["paused", "killed"]
ENTRYPOINTS = (
    [f"stage:{s}" for s in STAGE_ENTRYPOINTS]
    + [f"sandbox_job:{j}" for j in SANDBOX_JOB_ENTRYPOINTS]
    + [f"docker_filter:{op}" for op in DOCKER_FILTER_OPERATIONS]
)
CHECKPOINTS = [f"{kind}:{step}" for kind in OUTBOUND_KINDS for step in SUBMISSION_CHECKPOINTS[kind]]


def stop(ctx: AppContext, how: str) -> None:
    if how == "paused":
        ctx.control.pause("test", "uid:1000/operator")
    else:
        ctx.control.kill("test", "uid:1000/operator")


def edge_into(target: State) -> tuple[State, Actor]:
    for from_, targets in ALLOWED.items():
        if target in targets and from_ is not target:
            actor = sorted(targets[target])[0]
            return from_, actor
    raise AssertionError(f"no edge into {target}")


WORKING_TARGETS = sorted(BLOCKED_WHILE_STOPPED - {State.IMPORTED, State.DISCOVERED})  # created, never entered


def test_shared_constants_cover_every_kind() -> None:
    assert set(SUBMISSION_CHECKPOINTS) == set(OUTBOUND_KINDS)
    assert {"discover", "implement", "package", "watch"} <= set(STAGE_ENTRYPOINTS)
    assert {"kiro", "exec", "package", "unbundle"} <= set(SANDBOX_JOB_ENTRYPOINTS)


@pytest.mark.parametrize("how", STOPPED)
@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
def test_entrypoints_refused_when_stopped(ctx: AppContext, entrypoint: str, how: str) -> None:
    require_running(ctx.paths.control_dir, entrypoint)  # allowed while running
    stop(ctx, how)
    with pytest.raises(Blocked) as info:
        require_running(ctx.paths.control_dir, entrypoint)
    assert info.value.flags.state == how


@pytest.mark.parametrize("how", STOPPED)
@pytest.mark.parametrize("checkpoint", CHECKPOINTS)
def test_submission_checkpoints_refused_when_stopped(ctx: AppContext, checkpoint: str, how: str) -> None:
    ctx.control.checkpoint(checkpoint, snapshot_dry_run=True)
    stop(ctx, how)
    with pytest.raises(Blocked):
        ctx.control.checkpoint(checkpoint, snapshot_dry_run=True)


@pytest.mark.parametrize("checkpoint", CHECKPOINTS)
def test_checkpoint_refuses_mode_change(ctx: AppContext, checkpoint: str) -> None:
    """R4-N2: a dry-run toggle mid-submission stops the next write."""
    ctx.control.checkpoint(checkpoint, snapshot_dry_run=True)
    ctx.control.set_dry_run(False, "tg:4242")
    with pytest.raises(ModeChanged):
        ctx.control.checkpoint(checkpoint, snapshot_dry_run=True)


@pytest.mark.parametrize("how", STOPPED)
@pytest.mark.parametrize("target", WORKING_TARGETS, ids=str)
def test_transition_into_working_state_refused_when_stopped(ctx: AppContext, target: State, how: str) -> None:
    from_, actor = edge_into(target)
    cid = make(ctx, from_, **guard_columns(from_, target))
    stop(ctx, how)
    with pytest.raises(TransitionError) as info:
        ctx.state_machine.transition(cid, from_, target, actor)
    assert info.value.status == 423 and info.value.code == how


@pytest.mark.parametrize("how", STOPPED)
def test_stopping_moves_allowed_when_stopped(ctx: AppContext, how: str) -> None:
    cid = make(ctx, State.SUBMITTING)
    stop(ctx, how)
    assert (
        ctx.state_machine.transition(cid, State.SUBMITTING, State.SUBMIT_FAILED, Actor.GATEKEEPER).state
        is State.SUBMIT_FAILED
    )
    cid2 = make(ctx, State.VALIDATING)
    assert ctx.state_machine.transition(cid2, State.VALIDATING, State.FAILED, Actor.WORKER).state is State.FAILED


def test_levels_only_escalate(ctx: AppContext) -> None:
    ctx.control.kill("a", "x", "full")
    ctx.control.kill("b", "x", "soft")
    assert ctx.control.view().kill_level == "full"


def test_pause_while_killed_keeps_killed(ctx: AppContext) -> None:
    ctx.control.kill("a", "x")
    assert ctx.control.pause("b", "y").state == "killed"


def _audits(ctx: AppContext, event: str) -> list[dict[str, object]]:
    rows = ctx.conn.execute("SELECT data_json FROM audit_events WHERE event = ? ORDER BY seq", (event,))
    return [json.loads(r["data_json"]) for r in rows]


def test_set_egress_mode_writes_flag_audits_and_fyis(ctx: AppContext) -> None:
    """§11.5: the default comes from the snapshot (open); a switch writes {mode, actor, ts}."""
    default = ctx.config.egress.mode_default
    assert default == "open" and ctx.control.egress_mode(default) == "open"
    previous = ctx.control.set_egress_mode("allowlist", "uid:1000/operator", default)
    assert previous == "open" and ctx.control.egress_mode(default) == "allowlist"
    flag = json.loads((ctx.paths.control_dir / "egress-mode").read_text())
    assert flag["mode"] == "allowlist" and flag["actor"] == "uid:1000/operator" and flag["ts"]
    assert (ctx.paths.control_dir / "egress-mode").stat().st_mode & 0o777 == 0o640
    assert _audits(ctx, "egress_mode_changed") == [{"from": "open", "to": "allowlist"}]
    assert any("Egress mode open → allowlist" in t for t in outbox_texts(ctx))


async def test_egress_mode_api_is_operator_only(ctx: AppContext) -> None:
    for uid in (UID_WORKER, UID_SANDBOX, UID_REQUESTER):
        async with client(ctx, uid) as c:
            assert (await c.post("/v1/control/egress-mode", json={"mode": "allowlist"})).status_code == 403
            assert (await c.get("/v1/control/egress-mode")).json()["mode"] == "open"
    async with client(ctx, UID_OPERATOR) as c:
        assert (await c.post("/v1/control/egress-mode", json={"mode": "fetch"})).status_code == 400
        resp = await c.post("/v1/control/egress-mode", json={"mode": "research"})
    assert resp.status_code == 200
    assert resp.json() == {"mode": "research", "default": "open", "previous": "open"}
    assert _audits(ctx, "egress_mode_changed")[-1] == {"from": "open", "to": "research"}


def test_dry_run_default_on_and_resume_does_not_change_it(ctx: AppContext) -> None:
    assert ctx.control.view().dry_run is True
    ctx.control.pause("p", "x")
    ctx.control.request_resume("tg:4242")
    ctx.paths.paused_flag.unlink()
    ctx.paths.resume_request.unlink()
    ctx.control.reconcile()
    view = ctx.control.view()
    assert view.state == "running" and view.dry_run is True
