"""Delta 7 (DESIGN §14.18.4.3, AC 57): every re-created approval carries its own part count, and the
AppContext wrappers publish each new id once, after the commit."""

from __future__ import annotations

from typing import Any

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.outbox import OutboxWorker
from ossbot.gatekeeper.telegram.bot import CardPublisher
from tests.conftest import insert_action, packaged_contribution
from tests.unit.gatekeeper.decision_helpers import (
    RecordingSender,
    install_sources,
    proposal_card,
    proposed_contribution,
    skill_card,
)

LONG_BODY = "Body line with some words.\n" * 400 + "\n<!-- ossbot:cid=x --> AI assistance disclosure"


def _parts(ctx: AppContext, approval: str) -> tuple[int, int]:
    row = db.one(ctx.conn, "SELECT body_parts_total, body_parts_delivered FROM approvals WHERE id = ?", (approval,))
    assert row is not None
    return int(row["body_parts_total"]), int(row["body_parts_delivered"])


def _claim_and_pr(ctx: AppContext) -> tuple[str, str, str]:
    cid, pr = packaged_contribution(ctx)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE contributions SET pr_body = ? WHERE id = ?", (LONG_BODY, cid))
    claim = insert_action(ctx, cid, "issue_claim", {"target_number": 101, "body": "I'd like to work on this."})
    return cid, pr, claim


def test_each_action_gets_its_own_count(ctx: AppContext) -> None:
    cid, pr, claim = _claim_and_pr(ctx)
    new = ctx.reissue(cid, "tg:4242", keep_expiry=True)
    by_action = {
        db.one(ctx.conn, "SELECT action_id FROM approvals WHERE id = ?", (a,))["action_id"]: _parts(ctx, a)[0]  # type: ignore[index]
        for a in new
    }
    assert by_action[claim] == 1
    assert by_action[pr] >= 3  # body parts + the commit message


async def test_no_approve_before_every_part_and_claim_armed_after_its_one(ctx: AppContext) -> None:
    cid, pr, _ = _claim_and_pr(ctx)
    publisher = CardPublisher(ctx)
    ctx.on_approval_created = publisher.publish
    sender = RecordingSender()
    worker = OutboxWorker(ctx.outbox, sender, publisher.on_sent, batch=1)
    new = ctx.reissue(cid, "tg:4242", keep_expiry=True)
    pr_approval = next(a for a in new if db.one(ctx.conn, "SELECT action_id FROM approvals WHERE id = ?",
                                                    (a,))["action_id"] == pr)  # type: ignore[index]  # fmt: skip
    sender.fail_once = {3}  # some later part fails once (429), then succeeds
    for _ in range(60):
        approve = db.one(ctx.conn, "SELECT 1 FROM ui_tokens WHERE approval_id = ? AND purpose = 'approve'",
                         (pr_approval,))  # fmt: skip
        total, delivered = _parts(ctx, pr_approval)
        if approve is not None:
            assert delivered == total  # Approve exists only after every part
        await worker.run_once()
    assert _parts(ctx, pr_approval)[1] == _parts(ctx, pr_approval)[0]
    armed = [k for (k,) in ctx.conn.execute("SELECT dedupe_key FROM tg_outbox WHERE dedupe_key LIKE 'arm:%'")]
    assert sorted(armed) == sorted(f"arm:{a}" for a in new)


def test_wrappers_publish_each_id_once_after_the_commit(ctx: AppContext) -> None:
    install_sources(ctx)[1].staged["git-helper"] = "sr_1"
    seen: list[tuple[str, bool]] = []

    def hook(approval_id: str) -> None:
        seen.append((approval_id, ctx.conn.in_transaction))

    ctx.on_approval_created = hook
    cid, _, _ = _claim_and_pr(ctx)
    ids = ctx.reissue(cid, "tg:4242", keep_expiry=True)
    pcid = proposed_contribution(ctx)
    proposal_card(ctx, pcid)
    ids += ctx.reissue_proposal(pcid, "tg:4242", keep_expiry=True)
    skill_card(ctx, parts=3)
    ids += ctx.reissue_skill("git-helper", "tg:4242", keep_expiry=True)
    assert [i for i, _ in seen] == ids and len(set(ids)) == len(ids)
    assert not any(in_tx for _, in_tx in seen)  # never from inside a service transaction
    skill_row: Any = db.one(ctx.conn, "SELECT body_parts_total FROM skill_approvals WHERE id = ?", (ids[-1],))
    assert skill_row["body_parts_total"] == 3  # the skill card's own part count
    assert ctx.decisions.proposals.part_count(ids[-2]) == 0  # proposals arm with no parts
