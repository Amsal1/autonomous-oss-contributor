"""Gate 1 decision service (DESIGN §9.5b, §14.18.4.2): two taps, single-use message-bound tokens,
binding recomputed at Approve, Confirm and consume; refused while paused, killed, draining, down."""

from __future__ import annotations

import pytest

from ossbot.common.control_flags import write_flag
from ossbot.gatekeeper.approvals import ApprovalError, Superseded
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.decisions import DecisionServices
from tests.unit.gatekeeper.decision_helpers import (
    CHAT,
    install_sources,
    proposal_card,
    proposed_contribution,
    status_of,
)


def _start(ctx: AppContext, tokens: dict[str, str], message_id: int = 700) -> str:
    return ctx.decisions.proposals.approve(
        tokens["proposal_start"], tg_user_id=CHAT, chat_id=CHAT, message_id=message_id
    ).confirm_token


def test_two_taps_then_consume(ctx: AppContext) -> None:
    install_sources(ctx)
    cid = proposed_contribution(ctx)
    pid, tokens = proposal_card(ctx, cid)
    assert pid.startswith("p_") and set(tokens) == {"proposal_start"}
    confirm = _start(ctx, tokens)
    assert status_of(ctx, "proposal_approvals", pid) == "armed"
    binding = ctx.decisions.proposals.confirm(confirm, tg_user_id=CHAT, chat_id=CHAT, message_id=700)
    assert binding == {"v": 1, "kind": "proposal", "cid": cid, "version": 1, "proposal_sha256": "a" * 64,
                       "est_class": "s", "user_directed": False, "policy_hash": "h"}  # fmt: skip
    assert ctx.decisions.proposals.consume(pid) == binding
    assert status_of(ctx, "proposal_approvals", pid) == "consumed"


def test_tokens_are_single_use_and_message_bound(ctx: AppContext) -> None:
    install_sources(ctx)
    _, tokens = proposal_card(ctx, proposed_contribution(ctx))
    with pytest.raises(ApprovalError, match="token_wrong_message"):
        _start(ctx, tokens, message_id=701)
    confirm = _start(ctx, tokens)
    with pytest.raises(ApprovalError, match="token_used"):
        _start(ctx, tokens)
    with pytest.raises(ApprovalError, match="token_purpose"):
        ctx.decisions.proposals.approve(confirm, tg_user_id=CHAT, chat_id=CHAT, message_id=700)
    with pytest.raises(ApprovalError, match="token_purpose"):  # a PR-card purpose never resolves here
        ctx.approvals.approve(tokens["proposal_start"], tg_user_id=CHAT, chat_id=CHAT, message_id=700)


@pytest.mark.parametrize("point", ["approve", "confirm", "consume"])
def test_binding_recomputed_at_each_point(ctx: AppContext, point: str) -> None:
    source, _ = install_sources(ctx)
    cid = proposed_contribution(ctx)
    pid, tokens = proposal_card(ctx, cid)
    changed = {"proposal_sha256": "b" * 64, "est_class": "m", "user_directed": False, "policy_hash": "h"}
    if point == "approve":
        source.fields_by_key[(cid, 1)] = changed
        with pytest.raises(Superseded):
            _start(ctx, tokens)
    else:
        confirm = _start(ctx, tokens)
        if point == "confirm":
            source.fields_by_key[(cid, 1)] = changed
            with pytest.raises(Superseded):
                ctx.decisions.proposals.confirm(confirm, tg_user_id=CHAT, chat_id=CHAT, message_id=700)
        else:
            ctx.decisions.proposals.confirm(confirm, tg_user_id=CHAT, chat_id=CHAT, message_id=700)
            source.fields_by_key[(cid, 1)] = changed
            with pytest.raises(Superseded):
                ctx.decisions.proposals.consume(pid)
    assert status_of(ctx, "proposal_approvals", pid) == "invalidated"


@pytest.mark.parametrize("stop", ["paused", "killed", "draining", "down"])
def test_start_work_refused_while_stopped(ctx: AppContext, stop: str) -> None:
    install_sources(ctx)
    pid, tokens = proposal_card(ctx, proposed_contribution(ctx))
    if stop in {"paused", "killed"}:
        write_flag(ctx.paths.control_dir / stop, {"reason": "t", "level": "soft"})
    else:
        write_flag(ctx.paths.lifecycle_flag, {"state": stop, "scope": "workers", "reason": "t"})
    with pytest.raises(ApprovalError, match="approve_refused"):
        _start(ctx, tokens)
    assert status_of(ctx, "proposal_approvals", pid) == "pending"


def test_reject_reasons_and_cancel(ctx: AppContext) -> None:
    install_sources(ctx)
    pid, tokens = proposal_card(ctx, proposed_contribution(ctx))
    arm = ctx.decisions.proposals.approve(tokens["proposal_start"], tg_user_id=CHAT, chat_id=CHAT, message_id=700)
    ctx.decisions.proposals.cancel(arm.cancel_token, tg_user_id=CHAT, chat_id=CHAT, message_id=700)
    assert status_of(ctx, "proposal_approvals", pid) == "pending"
    with pytest.raises(ApprovalError, match="token_used"):
        ctx.decisions.proposals.confirm(arm.confirm_token, tg_user_id=CHAT, chat_id=CHAT, message_id=700)
    with pytest.raises(ApprovalError, match="bad_reason"):
        ctx.decisions.proposals.reject(pid, "whatever", "tg:4242")  # type: ignore[arg-type]
    ctx.decisions.proposals.reject(pid, "snoozed", "tg:4242")
    row = ctx.conn.execute("SELECT status, rejected_reason FROM proposal_approvals WHERE id = ?", (pid,)).fetchone()
    assert tuple(row) == ("rejected", "snoozed")


def test_fail_closed_without_proposal_content(ctx: AppContext) -> None:
    with pytest.raises(ApprovalError, match="proposal_unavailable"):
        ctx.decisions.proposals.create(proposed_contribution(ctx), 1, tg_chat_id=CHAT)


def test_for_id_dispatches_on_prefix(ctx: AppContext) -> None:
    d: DecisionServices = ctx.decisions
    assert d.for_id("a_0000000000") is d.approvals
    assert d.for_id("p_0000000000") is d.proposals
    assert d.for_id("s_0000000000") is d.skills
    with pytest.raises(ApprovalError, match="unknown_approval"):
        d.for_id("x_0000000000")
