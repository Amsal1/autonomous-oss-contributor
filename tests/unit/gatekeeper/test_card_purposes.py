"""Delta 3 (DESIGN §14.18.4.1): card purposes per action kind, single-use flag per purpose."""

from __future__ import annotations

import pytest

from ossbot.common.schemas import OutboundKind, State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import DECISION_PURPOSES, card_purposes
from ossbot.gatekeeper.context import AppContext
from tests.conftest import insert_action, insert_contribution, packaged_contribution

CHAT = 4242

GOLDEN: dict[tuple[str, bool], set[str]] = {
    ("open_pr", False): {"approve", "reject", "reject_reason", "changes", "explore", "files", "edit_text_pick"},
    ("open_pr", True): {"approve", "reject", "reject_reason", "changes", "explore", "interdiff", "edit_text_pick"},
    ("push_update", False): {"approve", "reject", "reject_reason", "changes", "explore", "files"},
    ("push_update", True): {"approve", "reject", "reject_reason", "changes", "explore", "files"},
    ("comment", False): {"approve", "reject", "reject_reason", "edit_text_pick"},
    ("issue_claim", False): {"approve", "reject", "reject_reason", "edit_text_pick"},
}


@pytest.mark.parametrize(("kind", "interdiff"), list(GOLDEN))
def test_golden_purpose_sets(kind: OutboundKind, interdiff: bool) -> None:
    assert set(card_purposes(kind, interdiff)) == GOLDEN[(kind, interdiff)]


def _minted(ctx: AppContext, action: str) -> dict[str, int]:
    approval = ctx.approvals.create(action, tg_chat_id=CHAT, body_parts_total=1)
    ctx.approvals.set_card_message(approval, 555)
    ctx.approvals.mark_part_delivered(approval)
    tokens = ctx.approvals.issue_card_tokens(approval)
    rows = ctx.conn.execute("SELECT purpose, single_use, expires_at FROM ui_tokens WHERE approval_id = ?", (approval,))
    appr = db.one(ctx.conn, "SELECT expires_at FROM approvals WHERE id = ?", (approval,))
    assert appr is not None
    out = {}
    for r in rows.fetchall():
        assert r["expires_at"] == appr["expires_at"]  # view tokens expire with the approval
        out[str(r["purpose"])] = int(r["single_use"])
    assert set(out) == set(tokens)
    return out


@pytest.mark.parametrize("interdiff", [False, True])
def test_open_pr_single_use_flags(ctx: AppContext, interdiff: bool) -> None:
    ctx.approvals.interdiff = lambda cid, rev: interdiff
    _, action = packaged_contribution(ctx)
    flags = _minted(ctx, action)
    assert set(flags) == GOLDEN[("open_pr", interdiff)]
    for purpose, single in flags.items():
        assert single == (1 if purpose in DECISION_PURPOSES else 0), purpose


def test_push_update_and_comment_flags(ctx: AppContext) -> None:
    _, push = packaged_contribution(ctx, pr_number=7)
    assert _minted(ctx, push) == {p: int(p in DECISION_PURPOSES) for p in GOLDEN[("push_update", False)]}
    cid = insert_contribution(ctx, State.WATCHING, pr_number=8)
    comment = insert_action(ctx, cid, "comment", {"target_number": 8, "body": "Thanks!"})
    assert _minted(ctx, comment) == {p: int(p in DECISION_PURPOSES) for p in GOLDEN[("comment", False)]}


def test_view_tokens_survive_checks_but_die_with_the_approval(ctx: AppContext) -> None:
    _, action = packaged_contribution(ctx)
    approval = ctx.approvals.create(action, tg_chat_id=CHAT, body_parts_total=1)
    ctx.approvals.set_card_message(approval, 555)
    ctx.approvals.mark_part_delivered(approval)
    tokens = ctx.approvals.issue_card_tokens(approval)
    for _ in range(3):  # multi-use: a view handler checks, never uses
        with db.transaction(ctx.conn):
            ctx.approvals.use_token(tokens["explore"], ("explore",), chat_id=CHAT, message_id=555)
    ctx.approvals.invalidate_contribution(ctx.conn.execute("SELECT cid FROM outbound_actions").fetchone()[0], "x")
    row = db.one(ctx.conn, "SELECT used_at FROM ui_tokens WHERE token = ?", (tokens["explore"],))
    assert row is not None and row["used_at"] is not None
