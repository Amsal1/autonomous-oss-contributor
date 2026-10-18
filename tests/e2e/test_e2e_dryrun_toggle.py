"""Dry-run toggle (AC 4/5 binding, DESIGN §22.4): an approval armed under DRY RUN is superseded by
/dryrun off; tapping the old Confirm publishes nothing; a fresh LIVE card is issued."""

from __future__ import annotations

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalError
from tests.e2e.conftest import E2E


def test_old_dry_run_confirm_is_superseded_by_dryrun_off(e2e: E2E) -> None:
    cid, approval = e2e.to_preview()
    e2e.ctx.approvals.set_card_message(approval, 10)
    total = db.one(e2e.ctx.conn, "SELECT body_parts_total FROM approvals WHERE id = ?", (approval,))[0]
    for _ in range(total):
        e2e.ctx.approvals.mark_part_delivered(approval)
    tokens = e2e.ctx.approvals.issue_card_tokens(approval)
    arm = e2e.ctx.approvals.approve(tokens["approve"], tg_user_id=4242, chat_id=4242, message_id=10)
    e2e.ctx.control.set_dry_run(False, "uid:1000/operator")
    with pytest.raises(ApprovalError):
        e2e.ctx.approvals.confirm(arm.confirm_token, tg_user_id=4242, chat_id=4242, message_id=10)
    assert e2e.writes() == [] and e2e.pusher.pushes == []
    old = db.one(e2e.ctx.conn, "SELECT status FROM approvals WHERE id = ?", (approval,))
    assert old is not None and old[0] == "invalidated"
    e2e.ctx.reissue(cid, "tg:4242", keep_expiry=False)  # what the superseded card's "Show current" does
    fresh = e2e.pending_approval(cid)
    assert fresh != approval
    assert db.one(e2e.ctx.conn, "SELECT dry_run_shown FROM approvals WHERE id = ?", (fresh,))[0] == 0
