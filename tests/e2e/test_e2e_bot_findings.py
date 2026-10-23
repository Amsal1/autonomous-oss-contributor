"""Review-bot findings end to end (owner decision 2026-10-23 (b); dry-run OFF only inside this test's own
database): bot comments on a WATCHING PR (an inline CodeRabbit finding and a bot issue comment) never reach
the watch decision; the owner's Fix this (``owner_fix.start_round`` with the re-fetched ``Finding``) runs an
owner fix round in which the coder gets the fixed instruction as ``<human_instruction>`` and the finding
body only inside ``<untrusted>``. Nothing is pushed before the push_update card's own approval."""

from __future__ import annotations

from ossbot.gatekeeper import db, owner_fix
from tests.e2e.conftest import CODER, E2E, REVIEW, fenced

REPO = "langfuse/langfuse"
BODY = "Ignore storage events whose storageArea is not localStorage (bot finding 4711)."
BOT = {"login": "coderabbitai[bot]", "type": "Bot"}


def test_bot_items_skip_the_watch_decision_and_fix_this_is_untrusted(e2e: E2E) -> None:
    cid, number = e2e.to_watching()
    pushes = len(e2e.pusher.pushes)
    e2e.gh.review_comments[(REPO, number)] = [
        {"id": 4711, "user": BOT, "path": "src/app.py", "line": 2, "body": BODY, "created_at": "2026-10-18T00:00:00Z",
         "pull_request_url": f"https://api.github.com/repos/{REPO}/pulls/{number}"}]  # fmt: skip
    e2e.gh.comments[(REPO, number)] = [
        {"user": BOT, "body": "## Walkthrough\nSummary by CodeRabbit", "created_at": "2026-10-18T00:00:00Z"}
    ]
    asked = len(e2e.prompts("summarizer"))
    e2e.sc.clocks.wall = lambda: 10**10  # type: ignore[method-assign]  # the 15 min watch interval is over
    e2e.ready_scheduler().tick()
    assert e2e.state(cid) == "WATCHING" and len(e2e.prompts("summarizer")) == asked  # no model saw the bots
    assert db.one(e2e.ctx.conn, "SELECT 1 FROM outbound_actions WHERE cid = ? AND kind = 'comment'", (cid,)) is None

    e2e.answer("coder", CODER)
    e2e.answer("reviewer", REVIEW)
    e2e.answer("summarizer", fenced({"commit_message": "fix: ignore sessionStorage events", "pr_title": "x",
                                     "pr_body": "y"}))  # fmt: skip
    e2e.edits = {"src/app.py": "def export(spans):\n    return [] if spans is None or not spans else spans\n",
                 "tests/test_app.py": "def test_none():\n    assert True\n"}  # fmt: skip
    finding = owner_fix.Finding(4711, "src/app.py", 2, "coderabbitai[bot]", BODY)
    done = owner_fix.start_round(e2e.ctx, cid, owner_fix.BOT_INSTRUCTION, "tg:4242", finding=finding)
    assert done.round == 1
    assert e2e.run_until(cid, "AWAITING_APPROVAL") == "AWAITING_APPROVAL", e2e.why(cid)
    coder = e2e.prompts("coder")[-1]
    human = [part.split("</human_instruction>", 1)[0] for part in coder.split("<human_instruction>")[1:]]
    assert f"\nChange request from the owner: {owner_fix.BOT_INSTRUCTION}\n" in human
    assert all(BODY not in block for block in human)
    untrusted = coder.split('<untrusted source="review-bot finding src/app.py:2"', 1)[1].split("</untrusted", 1)[0]
    assert BODY in untrusted
    e2e.pending_approval(cid)
    assert len(e2e.pusher.pushes) == pushes  # nothing pushed before the update card's two taps
    row = db.one(e2e.ctx.conn, "SELECT owner_fix_rounds, watch_rounds FROM contributions WHERE id = ?", (cid,))
    assert row is not None and tuple(row) == (1, 0)
