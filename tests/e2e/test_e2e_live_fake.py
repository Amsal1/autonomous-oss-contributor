"""AC 5 end to end against the fake upstream (dry-run OFF only inside this test's own database):
fork, push of the approved SHA, PR whose title/body hash to the approved values, commit identity
from the test profile; then watch: a maintainer question -> a pending ``comment`` card (WATCHING -> WATCHING),
a "please fix" comment -> fix round (unbundle of the approved bundle) -> ``push_update`` card."""

from __future__ import annotations

from ossbot.gatekeeper import db
from tests.e2e.conftest import E2E, fenced
from tests.githarness import tgit


def test_live_fake_pr_then_watch_reply_and_fix_round(e2e: E2E) -> None:
    e2e.ctx.control.set_dry_run(False, "uid:1000/operator")
    cid, approval = e2e.to_preview()
    assert e2e.approve(approval) == "done"
    row = db.one(e2e.ctx.conn, "SELECT state, pr_number, head_sha FROM contributions WHERE id = ?", (cid,))
    assert row is not None and row["pr_number"] is not None
    assert [p[1] for p in e2e.pusher.pushes] == [row["head_sha"]]
    pr = e2e.gh.pulls["langfuse/langfuse"][-1]
    assert pr["title"] == "fix: handle traces without spans" and f"ossbot:cid={cid}" in pr["body"]
    bundle = e2e.ctx.paths.artifacts_dir / cid / "1" / "change.bundle"
    _, head, branch = e2e.pusher.pushes[0]
    e2e.world.push_fork(bundle, head, branch)  # FakePusher records; the local fork plays the pushed branch
    mirror = e2e.world.root / "check"
    tgit(e2e.world.root, "clone", "-q", str(e2e.world.upstream), str(mirror))
    tgit(mirror, "fetch", "-q", str(bundle), "refs/heads/ossbot-change:refs/check")
    ident = tgit(mirror, "log", "-1", "--format=%an <%ae>|%cn <%ce>|%(trailers:key=Signed-off-by,valueonly)",
                 "refs/check")  # fmt: skip
    profile = e2e.ctx.config.profile  # the fixed identity (tests: tests/fixtures/profile.test.yaml)
    who = f"{profile.commit_name} <{profile.commit_email}>"
    assert ident.split("|")[0] == ident.split("|")[1] == who and who in ident.split("|")[2]
    assert e2e.run_until(cid, "WATCHING") == "WATCHING", e2e.why(cid)

    number = int(row["pr_number"])
    e2e.gh.comments[("langfuse/langfuse", number)] = [
        {"user": {"login": "maintainer"}, "body": "Why not return None?", "created_at": "2026-10-18T00:00:00Z"}
    ]
    e2e.answer("summarizer", {"action": "reply", "reply_body": "An empty list keeps callers simple."})
    e2e.sc.clocks.wall = lambda: 10**10  # type: ignore[method-assign]  # the 15 min watch interval is over
    e2e.ready_scheduler().tick()
    comment = db.one(e2e.ctx.conn, "SELECT status FROM outbound_actions WHERE cid = ? AND kind = 'comment'", (cid,))
    assert comment is not None and comment[0] == "pending_approval" and e2e.state(cid) == "WATCHING"

    e2e.gh.comments[("langfuse/langfuse", number)].append(
        {"user": {"login": "maintainer"}, "body": "Please also handle None.", "created_at": "2026-10-18T01:00:00Z"}
    )
    e2e.answer(
        "summarizer",
        {"action": "fix", "fix_request": "handle None too"},
        fenced({"commit_message": "fix: handle None spans", "pr_title": "x", "pr_body": "y"}),
    )
    e2e.edits = {"src/app.py": "def export(spans):\n    return [] if spans is None or not spans else spans\n",
                 "tests/test_app.py": "def test_none():\n    assert True\n"}  # fmt: skip
    e2e.sc.clocks.wall = lambda: 10**10 + 1000  # type: ignore[method-assign]
    assert e2e.run_until(cid, "AWAITING_APPROVAL") == "AWAITING_APPROVAL", e2e.why(cid)
    assert "unbundle:fix_round" in e2e.log
    push = db.one(e2e.ctx.conn, "SELECT status FROM outbound_actions WHERE cid = ? AND kind = 'push_update'", (cid,))
    assert push is not None and push[0] == "pending_approval"
    assert len(e2e.pusher.pushes) == 1  # nothing pushed before its own approval
