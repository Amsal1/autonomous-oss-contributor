"""Delta 2 (DESIGN §14.18.4, AC 29): a timed-out Confirm goes back to S1 with fresh tokens, on all
three decision services, through the housekeeping tick."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.app import housekeeping_once
from ossbot.gatekeeper.approvals import ApprovalError
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.bot import CardPublisher
from tests.conftest import packaged_contribution
from tests.unit.gatekeeper.decision_helpers import (
    CHAT,
    install_sources,
    proposal_card,
    proposed_contribution,
    skill_card,
    status_of,
)


def _pr_card(ctx: AppContext) -> tuple[str, dict[str, str]]:
    _, action = packaged_contribution(ctx)
    approval = ctx.approvals.create(action, tg_chat_id=CHAT, body_parts_total=1)
    ctx.approvals.set_card_message(approval, 555)
    ctx.approvals.mark_part_delivered(approval)
    return approval, ctx.approvals.issue_card_tokens(approval)


async def test_pr_confirm_timeout_disarms_to_s1(ctx: AppContext) -> None:
    publisher = CardPublisher(ctx)
    ctx.approvals.on_disarmed = publisher.render_s1
    approval, tokens = _pr_card(ctx)
    arm = ctx.approvals.approve(tokens["approve"], tg_user_id=CHAT, chat_id=CHAT, message_id=555)
    ctx.clock.advance(seconds=ctx.config.policy.limits.confirm_ttl_seconds - 1)  # type: ignore[attr-defined]
    await housekeeping_once(ctx)
    assert status_of(ctx, "approvals", approval) == "armed"
    ctx.clock.advance(seconds=2)  # type: ignore[attr-defined]
    await housekeeping_once(ctx)
    assert status_of(ctx, "approvals", approval) == "pending"
    with pytest.raises(ApprovalError, match="token_used"):
        ctx.approvals.confirm(arm.confirm_token, tg_user_id=CHAT, chat_id=CHAT, message_id=555)
    with pytest.raises(ApprovalError, match="token_used"):
        ctx.approvals.approve(tokens["approve"], tg_user_id=CHAT, chat_id=CHAT, message_id=555)
    fresh = db.one(
        ctx.conn, "SELECT token FROM ui_tokens WHERE approval_id = ? AND purpose = 'approve' AND used_at IS NULL",
        (approval,),
    )  # fmt: skip
    assert fresh is not None  # the card can be armed again
    ctx.approvals.approve(fresh["token"], tg_user_id=CHAT, chat_id=CHAT, message_id=555)
    edits = ctx.conn.execute(
        "SELECT dedupe_key, payload_json FROM tg_outbox WHERE dedupe_key LIKE 'disarm:%'"
    ).fetchall()
    assert len(edits) == 1 and edits[0]["dedupe_key"].startswith(f"disarm:{approval}:")
    payload = json.loads(edits[0]["payload_json"])
    assert payload["message_id"] == 555 and "inline_keyboard" in payload["reply_markup"]
    audit = ctx.conn.execute("SELECT data_json FROM audit_events WHERE event = 'approval_disarmed'").fetchall()
    assert [json.loads(r[0])["reason"] for r in audit] == ["confirm_expired"]


async def test_second_disarm_of_the_same_approval_gets_its_own_edit(ctx: AppContext) -> None:
    publisher = CardPublisher(ctx)
    ctx.approvals.on_disarmed = publisher.render_s1
    approval, _ = _pr_card(ctx)
    for _ in range(2):
        token = db.one(ctx.conn, "SELECT token FROM ui_tokens WHERE approval_id = ? AND purpose = 'approve'"
                       " AND used_at IS NULL", (approval,))["token"]  # type: ignore[index]  # fmt: skip
        ctx.approvals.approve(token, tg_user_id=CHAT, chat_id=CHAT, message_id=555)
        ctx.clock.advance(timedelta(minutes=10))  # type: ignore[attr-defined]
        await housekeeping_once(ctx)
    keys = [r[0] for r in ctx.conn.execute("SELECT dedupe_key FROM tg_outbox WHERE dedupe_key LIKE 'disarm:%'")]
    assert len(keys) == 2 and len(set(keys)) == 2  # BA-3: per-occurrence key (armed_at)


async def test_proposal_and_skill_disarm(ctx: AppContext) -> None:
    install_sources(ctx)
    cid = proposed_contribution(ctx)
    pid, ptok = proposal_card(ctx, cid)
    sid, stok = skill_card(ctx)
    ctx.decisions.proposals.approve(ptok["proposal_start"], tg_user_id=CHAT, chat_id=CHAT, message_id=700)
    ctx.decisions.skills.approve(stok["skill_approve"], tg_user_id=CHAT, chat_id=CHAT, message_id=800)
    ctx.clock.advance(timedelta(minutes=6))  # type: ignore[attr-defined]
    await housekeeping_once(ctx)
    assert status_of(ctx, "proposal_approvals", pid) == "pending"
    assert status_of(ctx, "skill_approvals", sid) == "pending"
