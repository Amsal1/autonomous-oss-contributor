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
    PrefsUpdate,
    State,
)
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.control import ModeChanged
from ossbot.gatekeeper.state_machine import ALLOWED, BLOCKED_WHILE_STOPPED, Actor, TransitionError
from tests.conftest import UID_OPERATOR, UID_REQUESTER, UID_SANDBOX, UID_WORKER, packaged_contribution
from tests.unit.gatekeeper.api_helpers import client
from tests.unit.gatekeeper.decision_helpers import (
    install_sources,
    proposal_card,
    proposed_contribution,
    skill_card,
    status_of,
)
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
    previous = ctx.control.set_egress_mode("allowlist", "uid:1000/operator", default, egress=ctx.config.egress)
    assert previous == "open" and ctx.control.egress_mode(default) == "allowlist"
    flag = json.loads((ctx.paths.control_dir / "egress-mode").read_text())
    assert flag["mode"] == "allowlist" and flag["actor"] == "uid:1000/operator" and flag["ts"]
    assert (ctx.paths.control_dir / "egress-mode").stat().st_mode & 0o777 == 0o640
    assert _audits(ctx, "egress_mode_changed") == [{"from": "open", "to": "allowlist"}]
    assert any("Egress mode open → allowlist" in t and "(fetch/validate unchanged)" in t for t in outbox_texts(ctx))


_UNTOUCHED_TABLES = (
    "approvals", "proposal_approvals", "skill_approvals", "contribution_holds", "discovery_prefs",
    "discovered_targets", "ui_tokens",
)  # fmt: skip


def _rows(ctx: AppContext) -> dict[str, list[tuple[object, ...]]]:
    return {t: [tuple(r) for r in ctx.conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in _UNTOUCHED_TABLES}


def test_switch_to_and_from_sandbox_open_invalidates_nothing(ctx: AppContext) -> None:
    """Design §12.2 / E7: the FYI names fetch/validate +/-web_public; approvals, holds, prefs and
    discovered_targets rows are byte-identical before and after (a mode switch invalidates nothing)."""
    install_sources(ctx)
    skill_card(ctx)
    cid, action = packaged_contribution(ctx)
    ctx.create_approval(action, "system")
    now = "2026-10-21T00:00:00Z"
    ctx.conn.execute(
        "INSERT INTO discovered_targets (repo, source, status, first_seen, updated_at)"
        " VALUES ('acme/tool', 'owner', 'kept', ?, ?)",
        (now, now),
    )
    ctx.conn.execute(
        "INSERT INTO contribution_holds (id, cid, reason, detail_json, held_by, held_at)"
        " VALUES ('h_1', ?, 'egress_anomaly', '{}', 'sandbox', ?)",
        (cid, now),
    )
    ctx.conn.commit()
    ctx.discovery_prefs.update(PrefsUpdate(autonomous_max_class="xs"), "tg:4242")
    before = _rows(ctx)
    for table in ("approvals", "skill_approvals", "contribution_holds", "discovery_prefs", "discovered_targets"):
        assert before[table], table
    default, egress = ctx.config.egress.mode_default, ctx.config.egress
    assert ctx.control.set_egress_mode("sandbox_open", "tg:4242", default, egress=egress) == "open"
    assert ctx.control.egress_mode(default) == "sandbox_open"
    assert _rows(ctx) == before
    assert ctx.control.set_egress_mode("allowlist", "tg:4242", default, egress=egress) == "sandbox_open"
    assert _rows(ctx) == before
    assert _audits(ctx, "egress_mode_changed") == [
        {"from": "open", "to": "sandbox_open"},
        {"from": "sandbox_open", "to": "allowlist"},
    ]
    texts = outbox_texts(ctx)
    assert any("Egress mode open → sandbox_open" in t and "(fetch/validate +web_public)" in t for t in texts)
    assert any("Egress mode sandbox_open → allowlist" in t and "(fetch/validate −web_public)" in t for t in texts)


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


def test_set_hermes_tier_audits_fyis_and_invalidates_skill_approvals(ctx: AppContext) -> None:
    """§15.5 + delta 9: a tier switch invalidates every open skill approval with tier_changed, in the
    same transaction as the flag write and its audit row; PR and proposal approvals are untouched."""
    install_sources(ctx)
    sid, tokens = skill_card(ctx)
    ctx.decisions.skills.approve(tokens["skill_approve"], tg_user_id=4242, chat_id=4242, message_id=800)
    sid2, _ = skill_card(ctx, skill="other", message_id=801, review_id="sr_2")
    _, action = packaged_contribution(ctx)
    pr = ctx.create_approval(action, "system")
    default = ctx.config.policy.hermes.capability_tier_default
    assert default == "self_improve" and ctx.control.hermes_tier(default) == "self_improve"
    previous, invalidated = ctx.control.set_hermes_tier("locked", "uid:1000/operator", default)
    assert previous == "self_improve" and ctx.control.hermes_tier(default) == "locked"
    assert {i.approval_id for i in invalidated} == {sid, sid2}
    rows = ctx.conn.execute("SELECT status, invalidated_reason FROM skill_approvals").fetchall()
    assert {tuple(r) for r in rows} == {("invalidated", "tier_changed")}
    assert status_of(ctx, "approvals", pr) == "pending"
    flag = json.loads((ctx.paths.control_dir / "hermes-tier").read_text())
    assert flag["tier"] == "locked" and flag["actor"] == "uid:1000/operator"
    [audit] = _audits(ctx, "hermes_tier_changed")
    assert audit["from"] == "self_improve" and audit["to"] == "locked" and set(audit["invalidated"]) == {sid, sid2}  # type: ignore[arg-type]
    assert any("Hermes tier self_improve → locked" in t for t in outbox_texts(ctx))


async def test_hermes_tier_api_is_operator_only(ctx: AppContext) -> None:
    for uid in (UID_WORKER, UID_SANDBOX, UID_REQUESTER):
        async with client(ctx, uid) as c:
            assert (await c.post("/v1/control/hermes-tier", json={"tier": "locked"})).status_code == 403
            view = (await c.get("/v1/control/hermes-tier")).json()
            assert view["tier"] == "self_improve" and "skills" in view["toolsets"]
    async with client(ctx, UID_OPERATOR) as c:
        assert (await c.post("/v1/control/hermes-tier", json={"tier": "god"})).status_code == 400
        resp = await c.post("/v1/control/hermes-tier", json={"tier": "research"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["tier"] == "research" and body["previous"] == "self_improve" and "skills" not in body["toolsets"]


def test_dry_run_default_on_and_resume_does_not_change_it(ctx: AppContext) -> None:
    assert ctx.control.view().dry_run is True
    ctx.control.pause("p", "x")
    ctx.control.request_resume("tg:4242")
    ctx.paths.paused_flag.unlink()
    ctx.paths.resume_request.unlink()
    ctx.control.reconcile()
    view = ctx.control.view()
    assert view.state == "running" and view.dry_run is True


# ------------------------------------------------------------- implement network (ADR-0024)
def test_implement_network_defaults_off_and_unknown_reads_off(ctx: AppContext) -> None:
    assert ctx.control.implement_network() == "off"
    assert ctx.control.lifecycle_status(egress_default="open", tier_default="locked")["implement_network"] == "off"
    # a value outside the three (impossible through the CHECK; simulated on a copy of the row) reads off
    ctx.conn.execute("PRAGMA ignore_check_constraints = ON")
    ctx.conn.execute("UPDATE control_state SET implement_network = 'everything' WHERE id = 1")
    ctx.conn.execute("PRAGMA ignore_check_constraints = OFF")
    assert ctx.control.implement_network() == "off"


def test_set_implement_network_audits_fyis_and_noop(ctx: AppContext) -> None:
    default = ctx.config.egress.mode_default
    assert ctx.control.set_implement_network("on_request", "tg:4242", default) == "off"
    assert ctx.control.implement_network() == "on_request"
    assert _audits(ctx, "implement_network_changed") == [{"from": "off", "to": "on_request", "egress_mode": "open"}]
    texts = outbox_texts(ctx)
    assert any("Implement network off → on_request by tg:4242 (inactive until egress mode sandbox_open)" in t
               for t in texts)  # fmt: skip
    assert ctx.control.set_implement_network("on_request", "tg:4242", default) == "on_request"  # no-op
    assert len(_audits(ctx, "implement_network_changed")) == 1
    ctx.control.set_egress_mode("sandbox_open", "tg:4242", default, egress=ctx.config.egress)
    ctx.control.set_implement_network("auto", "uid:1000/operator", default)
    assert _audits(ctx, "implement_network_changed")[-1] == {"from": "on_request", "to": "auto",
                                                             "egress_mode": "sandbox_open"}  # fmt: skip
    assert any("Implement network on_request → auto by uid:1000/operator (active)" in t for t in outbox_texts(ctx))
    with pytest.raises(ValueError):
        ctx.control.set_implement_network("on", "tg:4242", default)  # type: ignore[arg-type]


def test_implement_network_toggle_invalidates_nothing(ctx: AppContext) -> None:
    """R2.3: gate-1, gate-2 and skill approvals, holds, prefs and the egress mode are untouched."""
    install_sources(ctx)
    skill_card(ctx)
    proposal_card(ctx, proposed_contribution(ctx))
    cid, action = packaged_contribution(ctx)
    ctx.create_approval(action, "system")
    ctx.conn.execute(
        "INSERT INTO contribution_holds (id, cid, reason, detail_json, held_by, held_at)"
        " VALUES ('h_1', ?, 'egress_anomaly', '{}', 'sandbox', 't')",
        (cid,),
    )
    ctx.conn.commit()
    before = _rows(ctx)
    default = ctx.config.egress.mode_default
    for value in ("on_request", "auto", "off"):
        ctx.control.set_implement_network(value, "tg:4242", default)  # type: ignore[arg-type]
        assert _rows(ctx) == before
    assert ctx.control.egress_mode(default) == "open" and ctx.control.view().dry_run is True
