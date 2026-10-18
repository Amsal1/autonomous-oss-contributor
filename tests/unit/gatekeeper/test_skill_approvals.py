"""Skill activation decision service (DESIGN §14.15, §14.18.4.2): two taps, parts before Approve,
multi-use view tokens, refused while paused or killed, binding recomputed before the effect."""

from __future__ import annotations

import pytest

from ossbot.common.control_flags import write_flag
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalError, Superseded
from ossbot.gatekeeper.context import AppContext
from tests.unit.gatekeeper.decision_helpers import CHAT, install_sources, skill_card, status_of


def test_parts_before_approve(ctx: AppContext) -> None:
    install_sources(ctx)
    sid = ctx.decisions.skills.create("sr_1", "git-helper", 1, tg_chat_id=CHAT, body_parts_total=2)
    ctx.decisions.skills.set_card_message(sid, 800)
    with pytest.raises(ApprovalError, match="body_not_delivered"):
        ctx.decisions.skills.issue_card_tokens(sid)
    assert ctx.decisions.skills.part_count(sid) == 2


def test_two_taps_reject_and_views(ctx: AppContext) -> None:
    install_sources(ctx)
    sid, tokens = skill_card(ctx)
    assert set(tokens) == {"skill_approve", "skill_reject", "skill_diff", "skill_ask"}
    flags = dict(ctx.conn.execute("SELECT purpose, single_use FROM ui_tokens WHERE approval_id = ?", (sid,)).fetchall())
    assert flags == {"skill_approve": 1, "skill_reject": 1, "skill_diff": 0, "skill_ask": 0}
    arm = ctx.decisions.skills.approve(tokens["skill_approve"], tg_user_id=CHAT, chat_id=CHAT, message_id=800)
    binding = ctx.decisions.skills.confirm(arm.confirm_token, tg_user_id=CHAT, chat_id=CHAT, message_id=800)
    assert binding["kind"] == "skill" and binding["skill"] == "git-helper" and binding["content_sha256"] == "c" * 64
    assert ctx.decisions.skills.consume(sid) == binding
    sid2, tokens2 = skill_card(ctx, skill="other", message_id=801, review_id="sr_2")
    ctx.decisions.skills.reject(tokens2["skill_reject"], tg_user_id=CHAT, chat_id=CHAT, message_id=801)
    assert status_of(ctx, "skill_approvals", sid2) == "rejected"


def test_refused_while_paused_and_binding_rechecked(ctx: AppContext) -> None:
    _, skills = install_sources(ctx)
    sid, tokens = skill_card(ctx)
    write_flag(ctx.paths.paused_flag, {"reason": "t"})
    with pytest.raises(ApprovalError, match="approve_refused"):
        ctx.decisions.skills.approve(tokens["skill_approve"], tg_user_id=CHAT, chat_id=CHAT, message_id=800)
    ctx.paths.paused_flag.unlink()
    unused = db.one(ctx.conn, "SELECT used_at FROM ui_tokens WHERE token = ?", (tokens["skill_approve"],))
    assert unused is not None and unused["used_at"] is None  # a refused tap leaves the card tappable
    skills.fields_by_review["sr_1"] = {"content_sha256": "d" * 64, "tier": "self_improve"}
    with pytest.raises(Superseded):
        ctx.decisions.skills.approve(tokens["skill_approve"], tg_user_id=CHAT, chat_id=CHAT, message_id=800)
    assert status_of(ctx, "skill_approvals", sid) == "invalidated"


def test_fail_closed_without_staged_content(ctx: AppContext) -> None:
    with pytest.raises(ApprovalError, match="skill_unavailable"):
        ctx.decisions.skills.create("sr_9", "x", 1, tg_chat_id=CHAT, body_parts_total=1)
    with pytest.raises(ApprovalError, match="nothing_to_resend"):
        ctx.decisions.skills.reissue("x", actor="tg:4242", keep_expiry=True)
