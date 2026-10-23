"""Rerun with network (ADR-0024, design §4d): ``refusal`` is the state machine's guard plus not_found,
not_failed and stopped; ``start_rerun`` drives the human edge all or nothing and maps every TransitionError;
an unmapped code re-raises."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ossbot.common.schemas import State
from ossbot.common.yamlio import safe_load
from ossbot.gatekeeper import db, network_rerun
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.state_machine import TransitionError
from tests.conftest import FIXTURES, consumed_proposal, insert_contribution, write_flag
from tests.unit.gatekeeper.test_state_machine import GUARD_ORDER, _guard_case, allow_reruns

TABLE: list[dict[str, str]] = safe_load((FIXTURES / "implement_network_table.yaml").read_text(encoding="utf-8"))["rows"]


def failed(ctx: AppContext, **cols: Any) -> str:
    cols.setdefault("state_reason", "needs_network")
    cols.setdefault("proposal_version", 1)
    cid = insert_contribution(ctx, State.FAILED, upstream="runatlantis/atlantis", **cols)
    consumed_proposal(ctx, cid)
    return cid


def row(ctx: AppContext, cid: str) -> Any:
    found = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
    assert found is not None
    return found


def test_start_moves_the_state_and_audits_the_tap(ctx: AppContext) -> None:
    allow_reruns(ctx)
    cid = failed(ctx)
    assert network_rerun.refusal(ctx, cid) is None
    done = network_rerun.start_rerun(ctx, cid, "tg:4242")
    assert (done.cid, done.n, done.max) == (cid, 1, 1)
    c = row(ctx, cid)
    assert (c["state"], c["state_reason"], c["network_reruns"], c["network_grant"], c["network_grant_version"]) == (
        "PROPOSAL_APPROVED", "network_rerun", 1, 1, 1)  # fmt: skip
    event = db.one(ctx.conn, "SELECT actor, data_json FROM audit_events WHERE event = 'transition' AND cid = ?"
                   " ORDER BY seq DESC LIMIT 1", (cid,))  # fmt: skip
    assert event is not None and event["actor"] == "tg:4242"
    assert json.loads(event["data_json"])["evidence"] == {"reason": "network_rerun", "rerun": 1, "max": 1}
    with pytest.raises(network_rerun.RerunRefused) as info:  # a second tap: no longer failed
        network_rerun.start_rerun(ctx, cid, "tg:4242")
    assert info.value.code == "not_failed"


@pytest.mark.parametrize("code", GUARD_ORDER)
def test_refusal_is_the_guard(ctx: AppContext, code: str) -> None:
    cid = _guard_case(ctx, code)
    assert network_rerun.refusal(ctx, cid) == code
    with pytest.raises(network_rerun.RerunRefused) as info:
        network_rerun.start_rerun(ctx, cid, "tg:4242")
    assert info.value.code == code
    before = ctx.config.policy.limits.network_reruns_max if code == "network_reruns" else 0
    assert row(ctx, cid)["state"] == "FAILED" and row(ctx, cid)["network_reruns"] == before


@pytest.mark.parametrize("r", TABLE, ids=[f"{r['setting']}-{r['mode']}-{r['repo']}" for r in TABLE])
def test_column_ii(ctx: AppContext, r: dict[str, str]) -> None:
    default = ctx.config.egress.mode_default
    if r["mode"] != default:
        ctx.control.set_egress_mode(r["mode"], "tg:4242", default, egress=ctx.config.egress)  # type: ignore[arg-type]
    ctx.control.set_implement_network(r["setting"], "tg:4242", default)  # type: ignore[arg-type]
    if r["repo"] == "strict":
        ctx.conn.execute("INSERT INTO discovered_targets (repo, source, status, first_seen, updated_at,"
                         " egress_strict) VALUES ('acme/strict-tool', 'owner', 'kept', 't', 't', 1)")  # fmt: skip
        cid = insert_contribution(ctx, State.FAILED, upstream="acme/strict-tool", state_reason="needs_network",
                                  proposal_version=1)  # fmt: skip
        consumed_proposal(ctx, cid)
    else:
        cid = failed(ctx)
    assert network_rerun.refusal(ctx, cid) == (None if r["tap"] == "web" else r["tap"])


def test_not_found_not_failed_and_stopped(ctx: AppContext) -> None:
    allow_reruns(ctx)
    assert network_rerun.refusal(ctx, "c_0000000000") == "not_found"
    working = insert_contribution(ctx, State.IMPLEMENTING, upstream="runatlantis/atlantis")
    assert network_rerun.refusal(ctx, working) == "not_failed"
    cid = failed(ctx)
    write_flag(ctx.paths.paused_flag, {"reason": "x"})
    assert network_rerun.refusal(ctx, cid) == "stopped"
    with pytest.raises(network_rerun.RerunRefused) as info:
        network_rerun.start_rerun(ctx, cid, "tg:4242")
    assert info.value.code == "stopped" and row(ctx, cid)["state"] == "FAILED"


def test_a_423_from_the_state_machine_maps_to_stopped(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-check passed but the flag appeared before the edge: the state machine's 423 is a refusal."""
    allow_reruns(ctx)
    cid = failed(ctx)
    monkeypatch.setattr(ctx.approvals, "system_running", lambda: True)
    write_flag(ctx.paths.killed_flag, {"reason": "x"})
    with pytest.raises(network_rerun.RerunRefused) as info:
        network_rerun.start_rerun(ctx, cid, "tg:4242")
    assert info.value.code == "stopped" and row(ctx, cid)["network_reruns"] == 0


def test_unmapped_codes_reraise(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    allow_reruns(ctx)
    cid = failed(ctx)

    def boom(*args: object, **kwargs: object) -> None:
        raise TransitionError(409, "guard_failed:something_new")

    monkeypatch.setattr(ctx.state_machine, "transition", boom)
    with pytest.raises(TransitionError):
        network_rerun.start_rerun(ctx, cid, "tg:4242")
    assert row(ctx, cid)["state"] == "FAILED"
