"""Owner fix round end to end (ADR-0003 addendum 2026-10-23; dry-run OFF only inside this test's own
database): on a WATCHING PR the owner's confirm tap (``owner_fix.start_round``) inserts the change request
and moves WATCHING -> IMPLEMENTING as the human; the runner unbundles the approved bundle, the coder gets
the notes as ``<human_instruction>`` plus the fix-round instruction, and the round ends in a pending
``push_update`` card. Nothing is pushed before that card's own approval; branch_moved refuses the round."""

from __future__ import annotations

import pytest

from ossbot.gatekeeper import db, owner_fix
from ossbot.runner.stages.implement import FIX_ROUND_INSTRUCTION
from tests.e2e.conftest import CODER, E2E, REVIEW, fenced

NOTES = "Handle None in parse(); add a regression test."


def test_owner_fix_ends_in_a_pending_push_update(e2e: E2E) -> None:
    cid, _ = e2e.to_watching()
    pushes = len(e2e.pusher.pushes)
    summarizer_before = len(e2e.prompts("summarizer"))
    e2e.answer("coder", CODER)
    e2e.answer("reviewer", REVIEW)
    e2e.answer("summarizer", fenced({"commit_message": "fix: handle None spans", "pr_title": "x", "pr_body": "y"}))
    e2e.edits = {"src/app.py": "def export(spans):\n    return [] if spans is None or not spans else spans\n",
                 "tests/test_app.py": "def test_none():\n    assert True\n"}  # fmt: skip
    done = owner_fix.start_round(e2e.ctx, cid, NOTES, "tg:4242")  # what the confirm tap calls
    assert done.round == 1 and e2e.state(cid) == "IMPLEMENTING"
    assert e2e.run_until(cid, "AWAITING_APPROVAL") == "AWAITING_APPROVAL", e2e.why(cid)
    assert "unbundle:fix_round" in e2e.log
    push = db.one(e2e.ctx.conn, "SELECT status FROM outbound_actions WHERE cid = ? AND kind = 'push_update'", (cid,))
    assert push is not None and push[0] == "pending_approval"
    e2e.pending_approval(cid)  # the gate-2 card waits for its two taps
    assert len(e2e.pusher.pushes) == pushes  # nothing pushed before it
    coder = e2e.prompts("coder")[-1]
    assert f"<human_instruction>\nChange request from the owner: {NOTES}\n</human_instruction>" in coder
    assert FIX_ROUND_INSTRUCTION in coder
    row = db.one(e2e.ctx.conn, "SELECT owner_fix_rounds, watch_rounds FROM contributions WHERE id = ?", (cid,))
    assert row is not None and tuple(row) == (1, 0)
    request = db.one(e2e.ctx.conn, "SELECT consumed_at FROM change_requests WHERE id = ?", (done.change_request_id,))
    assert request is not None and request[0] is not None  # answered by the new rev ("You asked")
    asked = e2e.prompts("summarizer")[summarizer_before:]
    assert asked and not any("Decide what the new PR feedback needs" in p for p in asked)  # no watch decision


def test_branch_moved_refuses_the_owner_round(e2e: E2E) -> None:
    cid, _ = e2e.to_watching()
    with db.transaction(e2e.ctx.conn):
        e2e.ctx.conn.execute("UPDATE contributions SET fixes_disabled_reason = 'branch_moved' WHERE id = ?", (cid,))
    before = len(e2e.log)
    with pytest.raises(owner_fix.FixRefused) as info:
        owner_fix.start_round(e2e.ctx, cid, NOTES, "tg:4242")
    assert info.value.code == "fixes_disabled"
    assert e2e.state(cid) == "WATCHING"
    assert e2e.ctx.conn.execute("SELECT COUNT(*) FROM change_requests WHERE cid = ?", (cid,)).fetchone()[0] == 0
    e2e.ready_scheduler().tick()
    started = [e for e in e2e.log[before:] if e != "list_models"]
    assert e2e.state(cid) == "WATCHING" and started == []
