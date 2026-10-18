"""Delta 7 (DESIGN §14.18.4.3, AC 58): re-sends touch only eligible actions and never a sibling
that is mid-submission."""

from __future__ import annotations

import pytest

from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalError, reissue_eligible
from ossbot.gatekeeper.context import AppContext
from tests.conftest import insert_action, insert_contribution, packaged_contribution
from tests.unit.gatekeeper.decision_helpers import CHAT, status_of


def _set_state(ctx: AppContext, cid: str, state: State) -> None:
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE contributions SET state = ? WHERE id = ?", (state.value, cid))


def test_implementing_with_pending_r2_creates_nothing(ctx: AppContext) -> None:
    cid, action = packaged_contribution(ctx)
    ctx.create_approval(action, "system")
    _set_state(ctx, cid, State.IMPLEMENTING)
    with pytest.raises(ApprovalError) as info:
        ctx.reissue(cid, "tg:4242", keep_expiry=True)
    assert info.value.code == "nothing_to_resend" and "IMPLEMENTING" in str(info.value)


def test_after_the_expiry_sweep_retry_has_nothing_to_resend(ctx: AppContext) -> None:
    cid, action = packaged_contribution(ctx)
    ctx.create_approval(action, "system")
    ctx.clock.advance(hours=24 * 8)  # type: ignore[attr-defined]
    ctx.approvals.expire_due()
    assert ctx.state_machine.sweep_expired() == [cid]
    assert {r[0] for r in ctx.conn.execute("SELECT status FROM outbound_actions WHERE cid = ?", (cid,))} == {
        "cancelled"
    }
    with pytest.raises(ApprovalError) as info:
        ctx.reissue(cid, "tg:4242", keep_expiry=True)
    assert info.value.code == "nothing_to_resend" and "EXPIRED" in str(info.value)


def test_confirmed_sibling_refuses_and_stays_untouched(ctx: AppContext) -> None:
    cid, action = packaged_contribution(ctx)
    claim = insert_action(ctx, cid, "issue_claim", {"target_number": 101, "body": "Claim."})
    first = ctx.create_approval(claim, "system")
    ctx.approvals.set_card_message(first, 600)
    ctx.approvals.mark_part_delivered(first)
    tokens = ctx.approvals.issue_card_tokens(first)
    arm = ctx.approvals.approve(tokens["approve"], tg_user_id=CHAT, chat_id=CHAT, message_id=600)
    ctx.approvals.confirm(arm.confirm_token, tg_user_id=CHAT, chat_id=CHAT, message_id=600)
    pr = ctx.create_approval(action, "system")
    with pytest.raises(ApprovalError) as info:
        ctx.reissue(cid, "tg:4242", keep_expiry=True)
    assert info.value.code == "submitting"
    assert status_of(ctx, "approvals", first) == "confirmed" and status_of(ctx, "approvals", pr) == "pending"
    assert ctx.approvals.consume(first)["kind"] == "issue_claim"  # submits normally


@pytest.mark.parametrize(
    ("state", "kind"),
    [(State.VETTED, "issue_claim"), (State.WATCHING, "comment"), (State.AWAITING_APPROVAL, "issue_claim")],
)
def test_claims_and_replies_are_eligible_where_they_live(ctx: AppContext, state: State, kind: str) -> None:
    cid = insert_contribution(ctx, state, pr_number=8 if state is State.WATCHING else None)
    action = insert_action(ctx, cid, kind, {"target_number": 8, "body": "Text."})  # type: ignore[arg-type]
    ctx.create_approval(action, "system")
    assert len(ctx.reissue(cid, "tg:4242", keep_expiry=True)) == 1


LIVES_IN = {
    "open_pr": {State.AWAITING_APPROVAL},
    "push_update": {State.AWAITING_APPROVAL},
    "comment": {State.VETTED, State.PACKAGING, State.WATCHING, State.AWAITING_APPROVAL},
    "issue_claim": {State.VETTED, State.PACKAGING, State.WATCHING, State.AWAITING_APPROVAL},
}


@pytest.mark.parametrize("kind", list(LIVES_IN))
@pytest.mark.parametrize("state", list(State))
def test_predicate_table_every_kind_and_state(kind: str, state: State) -> None:
    want = state in LIVES_IN[kind]
    assert reissue_eligible(kind, "pending_approval", state) is want  # type: ignore[arg-type]
    for status in ("approved", "executing", "done", "dry_run_done", "failed", "cancelled"):
        assert reissue_eligible(kind, status, state) is False  # type: ignore[arg-type]  # 'approved' never written
