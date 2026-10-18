"""AC 28 / M4 end to end: a watched PR whose fix round fails self-review (revise, revise, reject) goes
back to WATCHING with ``fix_round_failed:<error_class>``, keeps its task volume, publishes nothing,
and the next maintainer comment is still handled."""

from __future__ import annotations

from ossbot.gatekeeper import db
from tests.e2e.conftest import E2E, REVIEW


def test_failed_fix_round_returns_to_watching(e2e: E2E) -> None:
    cid, number = e2e.to_watching()
    pushes = len(e2e.pusher.pushes)
    e2e.maintainer_says(number, "Please also handle None.", "2026-10-18T01:00:00Z")
    e2e.answer("summarizer", {"action": "fix", "fix_request": "handle None too"})
    revise = {"verdict": "revise", "blocking": [{"issue": "still wrong"}]}
    e2e.answer("reviewer", revise, revise, {"verdict": "reject", "blocking": [{"issue": "wrong approach"}]})
    e2e.ready_scheduler().tick()  # watch -> IMPLEMENTING
    assert e2e.run_until(cid, "WATCHING", ticks=40) == "WATCHING", e2e.why(cid)
    row = db.one(e2e.ctx.conn, "SELECT state_reason FROM contributions WHERE id = ?", (cid,))
    assert row is not None and str(row[0]).startswith("fix_round_failed:"), e2e.why(cid)
    assert e2e.has_task_volume(cid)  # kept for the next round (M9)
    assert len(e2e.pusher.pushes) == pushes and not db.one(
        e2e.ctx.conn, "SELECT 1 FROM outbound_actions WHERE cid = ? AND kind = 'push_update'", (cid,))  # fmt: skip
    e2e.answer("reviewer", REVIEW)
    e2e.maintainer_says(number, "Any update?", "2026-10-18T02:00:00Z")
    e2e.answer("summarizer", {"action": "reply", "reply_body": "Working on it."})
    e2e.ready_scheduler().tick()
    comment = db.one(e2e.ctx.conn, "SELECT status FROM outbound_actions WHERE cid = ? AND kind = 'comment'", (cid,))
    assert comment is not None and comment[0] == "pending_approval"
