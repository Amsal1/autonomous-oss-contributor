"""Submission and restart reconciliation against a fake GitHub (DESIGN §12.5, §12.5a, M2, M5, R4-N2,
AC 4, AC 5, AC 29, §22.1 test_submit.py)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from ossbot.common.hashing import sha256_text
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalError
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.github_read import GitHubError, GitHubReader
from ossbot.gatekeeper.reconcile import Reconciler
from ossbot.gatekeeper.state_machine import Actor
from ossbot.gatekeeper.submit import HELD_TEXT, Submitter
from tests.conftest import insert_action, insert_contribution
from tests.fakes.fake_github import FakeGitHub, FakePusher
from tests.fakes.holds import hold_request
from tests.githarness import GitWorld
from tests.unit.gatekeeper.test_package_verify import VETTED, package_ok

UPSTREAM = "langfuse/langfuse"
FORK = "test-bot-login/langfuse"


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def gh() -> FakeGitHub:
    fake = FakeGitHub()
    fake.add_repo(UPSTREAM)
    return fake


def notes(ctx: AppContext) -> list[str]:
    return [json.loads(r["payload_json"])["text"] for r in ctx.conn.execute("SELECT payload_json FROM tg_outbox")]


def confirm(ctx: AppContext, approval_id: str, message_id: int = 10) -> dict[str, Any]:
    ctx.approvals.set_card_message(approval_id, message_id)
    row = db.one(ctx.conn, "SELECT body_parts_total FROM approvals WHERE id = ?", (approval_id,))
    for _ in range(row["body_parts_total"]):
        ctx.approvals.mark_part_delivered(approval_id)
    tokens = ctx.approvals.issue_card_tokens(approval_id)
    arm = ctx.approvals.approve(tokens["approve"], tg_user_id=4242, chat_id=4242, message_id=message_id)
    return ctx.approvals.confirm(arm.confirm_token, tg_user_id=4242, chat_id=4242, message_id=message_id)


async def open_pr_ready(
    gctx: AppContext, world: GitWorld, gh: FakeGitHub, *, live: bool, issue: int = 77
) -> dict[str, Any]:
    if live:
        gctx.control.set_dry_run(False, "uid:1000/operator")
    gh.add_issue(UPSTREAM, issue)
    cid = insert_contribution(gctx, State.PACKAGING, issue_number=issue, vetting_json=VETTED)
    out = world.package(world.base, {"src/app.py": "print('submit')\n"})
    doc = await package_ok(gctx, world, cid, out)
    confirm(gctx, doc["approval_id"])
    return {"cid": cid, **doc}


def retry_action(ctx: AppContext, old_id: str) -> str:
    """What the Telegram Retry button does (handlers._retry): a fresh action and approval."""
    old = action(ctx, old_id)
    new_id = insert_action(ctx, old["cid"], old["kind"], json.loads(old["payload_json"]))
    ctx.state_machine.transition(old["cid"], State.SUBMIT_FAILED, State.AWAITING_APPROVAL, Actor.HUMAN, {})
    with db.transaction(ctx.conn):
        return ctx.create_approval(new_id, "tg:4242")


def submitter(
    ctx: AppContext, gh: FakeGitHub, pusher: FakePusher | None = None, token: str | None = "ghp_x"
) -> Submitter:
    return Submitter(ctx, gh.client(), pusher or FakePusher(gh), token=token, sleep=Sleeps())


def contribution(ctx: AppContext, cid: str) -> Any:
    return db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))


def action(ctx: AppContext, action_id: str) -> Any:
    return db.one(ctx.conn, "SELECT * FROM outbound_actions WHERE id = ?", (action_id,))


# --------------------------------------------------------------------------- happy paths
async def test_live_open_pr_creates_fork_pushes_and_opens_pr(gctx: AppContext, world: GitWorld, gh: FakeGitHub) -> None:
    doc = await open_pr_ready(gctx, world, gh, live=True)
    gh.fork_visible_after = 2
    sleeps = Sleeps()
    pusher = FakePusher(gh)
    sub = Submitter(gctx, gh.client(), pusher, token="ghp_x", sleep=sleeps)
    assert await sub.submit(doc["approval_id"]) == "done"
    assert [f"{w.method} {w.path}" for w in gh.writes] == [
        f"POST /repos/{UPSTREAM}/forks",
        f"PUSH {FORK}:{json.loads(action(gctx, doc['action_id'])['payload_json'])['branch']}",
        f"POST /repos/{UPSTREAM}/pulls",
    ]
    assert sleeps.calls == [3.0, 3.0]  # fork polling
    assert pusher.pushes[0][1] == doc["head_sha"]  # the exact approved SHA
    row = contribution(gctx, doc["cid"])
    assert row["state"] == "WATCHING"
    assert row["pr_number"] is not None
    assert row["head_sha"] == doc["head_sha"]
    act = action(gctx, doc["action_id"])
    assert act["status"] == "done"
    assert json.loads(act["result_json"])["writes"] == ["fork", "push", "pull_request"]
    assert any(n.startswith("Submitted ✅ PR #") for n in notes(gctx))


async def test_fork_reused_and_name_taken(gctx: AppContext, world: GitWorld, gh: FakeGitHub) -> None:
    doc = await open_pr_ready(gctx, world, gh, live=True)
    gh.add_repo(FORK, fork=False)  # the name is taken by a non-fork repo
    sub = submitter(gctx, gh)
    assert await sub.submit(doc["approval_id"]) == "done"
    fork_posts = [w for w in gh.writes if w.path.endswith("/forks")]
    assert [w.body["name"] for w in fork_posts] == ["langfuse-ossbot"]
    result = json.loads(action(gctx, doc["action_id"])["result_json"])
    assert result["fork"] == "test-bot-login/langfuse-ossbot"


async def test_existing_fork_reused(gctx: AppContext, world: GitWorld, gh: FakeGitHub) -> None:
    doc = await open_pr_ready(gctx, world, gh, live=True)
    gh.add_repo(FORK, fork=True, parent={"full_name": UPSTREAM})
    assert await submitter(gctx, gh).submit(doc["approval_id"]) == "done"
    assert not [w for w in gh.writes if w.path.endswith("/forks")]
    assert json.loads(action(gctx, doc["action_id"])["result_json"])["writes"] == ["push", "pull_request"]


async def test_stored_bytes_equal_hashed_equal_posted(gctx: AppContext, world: GitWorld, gh: FakeGitHub) -> None:
    """N3/M2: a CRLF body edit is stored as LF; the bytes posted equal the stored and hashed bytes."""
    gctx.control.set_dry_run(False, "uid:1000/operator")
    gh.add_issue(UPSTREAM, 78)
    cid = insert_contribution(gctx, State.PACKAGING, issue_number=78, vetting_json=VETTED)
    out = world.package(world.base, {"src/app.py": "print('n3')\n"})
    doc = await package_ok(gctx, world, cid, out)
    edited = f"Edited body\r\nline two\r\n\n---\nAI assistance disclosure\n<!-- ossbot:cid={cid} -->"
    gctx.approvals.set_text_override(cid, body=edited, actor="tg:4242")
    (new_approval,) = gctx.approvals.reissue(cid, tg_chat_id=4242, actor="tg:4242", part_count=lambda a: 2)
    binding = confirm(gctx, new_approval, message_id=11)
    gh.add_repo(FORK, fork=True, parent={"full_name": UPSTREAM})
    assert await submitter(gctx, gh).submit(new_approval) == "done"
    posted = next(w.body for w in gh.writes if w.path.endswith("/pulls"))
    stored = contribution(gctx, cid)["pr_body_override"]
    assert "\r" not in stored
    assert posted["body"] == stored
    assert sha256_text(posted["body"]) == binding["body_sha256"]
    assert sha256_text(posted["title"]) == binding["title_sha256"]
    assert posted["head"] == f"test-bot-login:{json.loads(action(gctx, doc['action_id'])['payload_json'])['branch']}"
    assert posted["maintainer_can_modify"] is True
    assert posted["draft"] is False


async def test_dry_run_makes_zero_write_calls(gctx: AppContext, world: GitWorld, gh: FakeGitHub) -> None:
    """AC 4."""
    doc = await open_pr_ready(gctx, world, gh, live=False)
    pusher = FakePusher(gh)
    assert await submitter(gctx, gh, pusher).submit(doc["approval_id"]) == "dry_run_done"
    assert gh.writes == []
    assert pusher.pushes == []
    assert {r.method for r in gh.requests} == {"GET"}
    assert contribution(gctx, doc["cid"])["state"] == "DRY_RUN_DONE"
    result = json.loads(action(gctx, doc["action_id"])["result_json"])
    assert set(result["would"]) == {"fork", "push", "pull_request"}
    assert result["would"]["pull_request"]["title"] == "fix(web): handle empty spans"
    event = db.one(gctx.conn, "SELECT data_json FROM audit_events WHERE event = 'dry_run_would'")
    assert event is not None
    assert any("DRY RUN — nothing was published" in n for n in notes(gctx))


async def test_pr_already_exists_reconciled(gctx: AppContext, world: GitWorld, gh: FakeGitHub) -> None:
    doc = await open_pr_ready(gctx, world, gh, live=True)
    gh.add_repo(FORK, fork=True, parent={"full_name": UPSTREAM})
    branch = json.loads(action(gctx, doc["action_id"])["payload_json"])["branch"]
    existing = gh.add_pull(UPSTREAM, user={"login": "test-bot-login"}, head={"label": f"test-bot-login:{branch}",
                                                                           "ref": branch})  # fmt: skip
    existing["state"] = "closed"  # not open: does not count as a competing/own open PR, but the POST still 422s
    assert await submitter(gctx, gh).submit(doc["approval_id"]) == "done"
    assert contribution(gctx, doc["cid"])["pr_number"] == existing["number"]
    assert any("already existed" in n for n in notes(gctx))


async def test_transient_failures_retried_then_partial_failure_skips_push(
    gctx: AppContext, world: GitWorld, gh: FakeGitHub
) -> None:
    doc = await open_pr_ready(gctx, world, gh, live=True)
    gh.add_repo(FORK, fork=True, parent={"full_name": UPSTREAM})
    gh.failures[f"POST /repos/{UPSTREAM}/pulls"] = [502, 502, 502, 502]
    pusher = FakePusher(gh)
    sleeps = Sleeps()
    sub = Submitter(gctx, gh.client(), pusher, token="ghp_x", sleep=sleeps)
    assert await sub.submit(doc["approval_id"]) == "failed"
    assert sleeps.calls == [2.0, 8.0, 30.0]
    act = action(gctx, doc["action_id"])
    assert (act["status"], act["error_class"]) == ("failed", "pull_request_failed")
    assert contribution(gctx, doc["cid"])["state"] == "SUBMIT_FAILED"
    assert len(pusher.pushes) == 1
    # Retry = fresh action + approval; the push is skipped because the fork ref already equals head
    new_approval = retry_action(gctx, doc["action_id"])
    confirm(gctx, new_approval, message_id=12)
    assert await submitter(gctx, gh, pusher).submit(new_approval) == "done"
    assert len(pusher.pushes) == 1


async def test_github_auth_failure_pauses(gctx: AppContext, world: GitWorld, gh: FakeGitHub) -> None:
    doc = await open_pr_ready(gctx, world, gh, live=True)
    gh.add_repo(FORK, fork=True, parent={"full_name": UPSTREAM})
    gh.failures[f"POST /repos/{UPSTREAM}/pulls"] = [401]
    assert await submitter(gctx, gh).submit(doc["approval_id"]) == "failed"
    assert action(gctx, doc["action_id"])["error_class"] == "github_auth"
    assert gctx.control.view().state == "paused"


# ------------------------------------------------------------------------------ identity
@pytest.mark.parametrize(("login", "scopes", "error", "state"), [
    ("test-work-login", "public_repo", "token_login_mismatch", "killed"),
    ("somebody-else", "public_repo", "token_login_mismatch", "killed"),
    ("test-bot-login", "read:user", "token_scope", "paused"),
])  # fmt: skip
async def test_token_identity_checked(
    gctx: AppContext, world: GitWorld, gh: FakeGitHub, login: str, scopes: str, error: str, state: str
) -> None:
    doc = await open_pr_ready(gctx, world, gh, live=True)
    gh.login, gh.scopes = login, scopes
    pusher = FakePusher(gh)
    assert await submitter(gctx, gh, pusher).submit(doc["approval_id"]) == "failed"
    assert gh.writes == []
    assert pusher.pushes == []
    assert action(gctx, doc["action_id"])["error_class"] == error
    assert gctx.control.view().state == state
    assert contribution(gctx, doc["cid"])["state"] == "SUBMIT_FAILED"


async def test_missing_write_token_refused(gctx: AppContext, world: GitWorld, gh: FakeGitHub) -> None:
    doc = await open_pr_ready(gctx, world, gh, live=True)
    assert await submitter(gctx, gh, token=None).submit(doc["approval_id"]) == "refused"
    assert gh.requests == []
    appr = db.one(gctx.conn, "SELECT status, invalidated_reason FROM approvals WHERE id = ?", (doc["approval_id"],))
    assert (appr["status"], appr["invalidated_reason"]) == ("invalidated", "no_write_token")
    act = action(gctx, doc["action_id"])
    assert (act["status"], act["error_class"]) == ("pending_approval", "no_write_token")
    assert contribution(gctx, doc["cid"])["state"] == "AWAITING_APPROVAL"


async def test_submit_requires_confirmed_approval(gctx: AppContext, world: GitWorld, gh: FakeGitHub) -> None:
    doc = await open_pr_ready(gctx, world, gh, live=True)
    assert await submitter(gctx, gh).submit(doc["approval_id"]) == "done"
    with pytest.raises(ApprovalError):  # single use: consumed
        await submitter(gctx, gh).submit(doc["approval_id"])


# ------------------------------------------------------------------------- interruption
def _flip(ctx: AppContext, how: str) -> Callable[[], None]:
    def go() -> None:
        if how == "kill":
            ctx.control.kill("test kill", "tg:4242")
        elif how == "pause":
            ctx.control.pause("test pause", "tg:4242")
        else:
            ctx.control.set_dry_run(True, "tg:4242")

    return go


@pytest.mark.parametrize("how", ["kill", "pause"])
async def test_kill_between_push_and_pr(gctx: AppContext, world: GitWorld, gh: FakeGitHub, how: str) -> None:
    """AC 29: the flag lands after the push; no PR call follows."""
    doc = await open_pr_ready(gctx, world, gh, live=True)
    gh.add_repo(FORK, fork=True, parent={"full_name": UPSTREAM})
    flip = _flip(gctx, how)
    gh.on_write = lambda what: flip() if what == "PUSH" else None
    assert await submitter(gctx, gh).submit(doc["approval_id"]) == "failed"
    assert not [w for w in gh.writes if w.path.endswith("/pulls")]
    act = action(gctx, doc["action_id"])
    assert (act["status"], act["error_class"]) == ("failed", "interrupted")
    result = json.loads(act["result_json"])
    assert result["writes"] == ["push"]
    assert result["cause"] == how
    assert contribution(gctx, doc["cid"])["state"] == "SUBMIT_FAILED"
    assert any(f"Submission interrupted by {how} after push" in n and "[Retry]" in n for n in notes(gctx))


async def test_hold_between_push_and_pr_then_retry_while_held(
    gctx: AppContext, world: GitWorld, gh: FakeGitHub
) -> None:
    """Open-discovery §13.3: a hold set after the push stops before the PR (cause held, its own text);
    [Retry] while the hold is open stops before the first write; FakeGitHub writes are unchanged."""
    doc = await open_pr_ready(gctx, world, gh, live=True)
    gh.add_repo(FORK, fork=True, parent={"full_name": UPSTREAM})

    def hold_after_push(what: str) -> None:
        if what == "PUSH":
            gctx.holds.hold(doc["cid"], hold_request(), "uid:2002/sandbox")

    gh.on_write = hold_after_push
    assert await submitter(gctx, gh).submit(doc["approval_id"]) == "failed"
    assert not [w for w in gh.writes if w.path.endswith("/pulls")]
    act = action(gctx, doc["action_id"])
    result = json.loads(act["result_json"])
    assert (act["error_class"], result["cause"], result["writes"]) == ("interrupted", "held", ["push"])
    assert contribution(gctx, doc["cid"])["state"] == "SUBMIT_FAILED"
    assert HELD_TEXT.format(done="push") in notes(gctx)
    gh.on_write = None
    writes = list(gh.writes)
    retry = retry_action(gctx, doc["action_id"])
    confirm(gctx, retry, message_id=11)
    assert await submitter(gctx, gh).submit(retry) == "failed"
    assert gh.writes == writes
    retried = action(gctx, str(db.one(gctx.conn, "SELECT action_id FROM approvals WHERE id = ?", (retry,))[0]))
    assert json.loads(retried["result_json"])["cause"] == "held"
    assert json.loads(retried["result_json"])["writes"] == []
    assert HELD_TEXT.format(done="no writes") in notes(gctx)


async def test_pause_before_fork(gctx: AppContext, world: GitWorld, gh: FakeGitHub) -> None:
    doc = await open_pr_ready(gctx, world, gh, live=True)
    sub = submitter(gctx, gh)
    original = sub._live

    async def live_then_pause(snap: Any) -> None:
        await original(snap)
        gctx.control.pause("between checks and fork", "tg:4242")

    sub._live = live_then_pause  # type: ignore[method-assign]
    assert await sub.submit(doc["approval_id"]) == "failed"
    assert gh.writes == []
    assert json.loads(action(gctx, doc["action_id"])["result_json"])["writes"] == []


async def test_dryrun_on_mid_submission_stops_write(gctx: AppContext, world: GitWorld, gh: FakeGitHub) -> None:
    """R4-N2: /dryrun on lands after the fork; no push or PR follows."""
    doc = await open_pr_ready(gctx, world, gh, live=True)
    flip = _flip(gctx, "dryrun")
    gh.on_write = lambda what: flip() if what.endswith("/forks") else None
    pusher = FakePusher(gh)
    assert await submitter(gctx, gh, pusher).submit(doc["approval_id"]) == "failed"
    assert pusher.pushes == []
    assert not [w for w in gh.writes if w.path.endswith("/pulls")]
    act = action(gctx, doc["action_id"])
    result = json.loads(act["result_json"])
    assert (act["error_class"], result["cause"], result["writes"]) == ("interrupted", "mode_changed", ["fork"])
    assert contribution(gctx, doc["cid"])["state"] == "SUBMIT_FAILED"
    assert any("mode changed during submission (now DRY RUN)" in n for n in notes(gctx))


# --------------------------------------------------------------------------- push_update
async def push_update_ready(gctx: AppContext, world: GitWorld, gh: FakeGitHub) -> dict[str, Any]:
    from tests.unit.gatekeeper.test_package_verify import fix_round_ready

    gctx.control.set_dry_run(False, "uid:1000/operator")
    cid = insert_contribution(gctx, State.PACKAGING, issue_number=79, vetting_json=VETTED)
    first = world.package(world.base, {"src/app.py": "print('pu1')\n"})
    doc1 = await package_ok(gctx, world, cid, first)
    await fix_round_ready(gctx, world, cid, first, doc1["action_id"])
    second = world.package(first.head, {"src/app.py": "print('pu2')\n"})
    doc = await package_ok(gctx, world, cid, second, rev=2, title=None, body=None)
    branch = contribution(gctx, cid)["branch"]
    gh.refs[(FORK, branch)] = first.head
    gh.add_repo(FORK, fork=True, parent={"full_name": UPSTREAM})
    head = {"label": f"test-bot-login:{branch}", "ref": branch}
    gh.pulls[UPSTREAM.lower()] = [
        {"number": 41, "state": "open", "user": {"login": "test-bot-login"}, "title": "", "body": "", "head": head}
    ]
    confirm(gctx, doc["approval_id"])
    return {"cid": cid, "old": first.head, "branch": branch, **doc}


async def test_push_update_live(gctx: AppContext, world: GitWorld, gh: FakeGitHub) -> None:
    doc = await push_update_ready(gctx, world, gh)
    pusher = FakePusher(gh)
    assert await submitter(gctx, gh, pusher).submit(doc["approval_id"]) == "done"
    assert pusher.pushes == [(FORK, doc["head_sha"], doc["branch"])]
    row = contribution(gctx, doc["cid"])
    assert (row["state"], row["head_sha"]) == ("WATCHING", doc["head_sha"])


async def test_push_update_branch_moved_at_submit(gctx: AppContext, world: GitWorld, gh: FakeGitHub) -> None:
    doc = await push_update_ready(gctx, world, gh)
    gh.refs[(FORK, doc["branch"])] = "e" * 40
    pusher = FakePusher(gh)
    assert await submitter(gctx, gh, pusher).submit(doc["approval_id"]) == "failed"
    assert pusher.pushes == []
    row = contribution(gctx, doc["cid"])
    assert row["state"] == "SUBMIT_FAILED"
    assert row["watch_rounds"] == gctx.config.policy.limits.watch_rounds_max
    assert action(gctx, doc["action_id"])["error_class"] == "branch_moved"


async def test_push_update_dryrun_toggle_before_push(gctx: AppContext, world: GitWorld, gh: FakeGitHub) -> None:
    doc = await push_update_ready(gctx, world, gh)
    sub = submitter(gctx, gh)
    original = sub._live

    async def live_then_toggle(snap: Any) -> None:
        await original(snap)
        gctx.control.set_dry_run(True, "tg:4242")

    sub._live = live_then_toggle  # type: ignore[method-assign]
    assert await sub.submit(doc["approval_id"]) == "failed"
    assert gh.writes == []
    assert json.loads(action(gctx, doc["action_id"])["result_json"])["cause"] == "mode_changed"


# ------------------------------------------------------------------------------- comments
async def test_comment_live_and_dry_run(ctx: AppContext, gh: FakeGitHub) -> None:
    for live in (False, True):
        if live:
            ctx.control.set_dry_run(False, "uid:1000/operator")
        cid = insert_contribution(ctx, State.WATCHING, pr_number=90)
        gh.add_issue(UPSTREAM, 90, pull_request={"url": "x"})
        action_id = insert_action(
            ctx, cid, "comment", {"target_number": 90, "body": "Thanks!\n\n<!-- ossbot:action=x -->"}
        )
        approval = ctx.create_approval(action_id, "test")
        confirm(ctx, approval, message_id=20 + int(live))
        before = len(gh.writes)
        status = await submitter(ctx, gh).submit(approval)
        assert status == ("done" if live else "dry_run_done")
        assert len(gh.writes) - before == (1 if live else 0)
        assert contribution(ctx, cid)["state"] == "WATCHING"


# ----------------------------------------------------------------- restart reconciliation
def executing(ctx: AppContext, kind: str, *, dry_run: bool = False) -> tuple[str, str]:
    state = State.SUBMITTING if kind in {"open_pr", "push_update"} else State.WATCHING
    cid = insert_contribution(ctx, state, issue_number=55 if kind != "push_update" else None,
                              pr_number=41 if kind != "open_pr" else None, branch="ossbot/55-fix-abc123")  # fmt: skip
    pr = {"rev": 1, "head_sha": "3" * 40, "branch": "ossbot/55-fix-abc123", "fork_owner": "test-bot-login"}
    payloads: dict[str, dict[str, Any]] = {
        "open_pr": pr,
        "push_update": {
            **pr,
            "rev": 2,
            "head_sha": "4" * 40,
            "pr_number": 41,
            "old_head_sha": "9" * 40,
            "fork_repo": FORK,
        },
        "comment": {"target_number": 41, "body": "hi"},
        "issue_claim": {"target_number": 55, "body": "claim"},
    }
    action_id = insert_action(
        ctx, cid, kind, payloads[kind], status="executing", executed_at="2026-10-17T11:00:00.000000Z"
    )  # type: ignore[arg-type]
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO approvals (id, action_id, binding_json, binding_hash, dry_run_shown, status, tg_chat_id,"
            " body_parts_total, created_at, expires_at, consumed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"a_{action_id[2:]}", action_id, json.dumps({"dry_run": dry_run}), "h", int(dry_run), "consumed", 4242, 1,
             "2026-10-17T10:00:00.000000Z", "2026-10-20T10:00:00.000000Z", "2026-10-17T11:00:00.000000Z"),
        )  # fmt: skip
    return cid, action_id


Setup = Callable[[FakeGitHub, str], None]


def _comment_found(gh: FakeGitHub, action_id: str) -> None:
    body = f"hi\n<!-- ossbot:action={action_id} -->"
    gh.comments[(UPSTREAM, 41)] = [{"user": {"login": "test-bot-login"}, "body": body, "html_url": "https://x/c/1"}]


def _pr_found(gh: FakeGitHub, action_id: str) -> None:
    gh.add_pull(UPSTREAM, user={"login": "test-bot-login"},
                head={"label": "test-bot-login:ossbot/55-fix-abc123", "ref": "ossbot/55-fix-abc123"})  # fmt: skip


@pytest.mark.parametrize(("kind", "setup", "outcome", "state"), [
    ("open_pr", _pr_found, "done", "WATCHING"),
    ("open_pr", lambda gh, a: None, "interrupted", "SUBMIT_FAILED"),
    ("push_update", lambda gh, a: gh.refs.__setitem__((FORK, "ossbot/55-fix-abc123"), "4" * 40), "done", "WATCHING"),
    ("push_update", lambda gh, a: gh.refs.__setitem__((FORK, "ossbot/55-fix-abc123"), "9" * 40), "interrupted",
     "SUBMIT_FAILED"),
    ("push_update", lambda gh, a: gh.refs.__setitem__((FORK, "ossbot/55-fix-abc123"), "8" * 40), "branch_moved",
     "SUBMIT_FAILED"),
    ("comment", _comment_found, "done", "WATCHING"),
    ("comment", lambda gh, a: None, "interrupted", "WATCHING"),
])  # fmt: skip
async def test_restart_reconciles_executing(
    ctx: AppContext, gh: FakeGitHub, kind: str, setup: Setup, outcome: str, state: str
) -> None:
    cid, action_id = executing(ctx, kind)
    setup(gh, action_id)
    outcomes = await Reconciler(ctx, GitHubReader(gh.client())).run_once()
    assert [(o.action_id, o.result) for o in outcomes] == [(action_id, outcome)]
    assert gh.requests
    assert {r.method for r in gh.requests} == {"GET"}  # reads only
    act = action(ctx, action_id)
    assert act["status"] == ("done" if outcome == "done" else "failed")
    assert contribution(ctx, cid)["state"] == state
    if outcome == "branch_moved":
        assert contribution(ctx, cid)["watch_rounds"] == ctx.config.policy.limits.watch_rounds_max


async def test_reconcile_dry_run_needs_no_reads(ctx: AppContext, gh: FakeGitHub) -> None:
    cid, _ = executing(ctx, "open_pr", dry_run=True)
    outcomes = await Reconciler(ctx, GitHubReader(gh.client())).run_once()
    assert outcomes[0].result == "interrupted"
    assert gh.requests == []
    assert contribution(ctx, cid)["state"] == "SUBMIT_FAILED"


async def test_reconcile_read_failure_keeps_pending(ctx: AppContext, gh: FakeGitHub) -> None:
    cid, action_id = executing(ctx, "open_pr")
    gh.failures[f"GET /repos/{UPSTREAM}/pulls"] = [503]
    outcomes = await Reconciler(ctx, GitHubReader(gh.client())).run_once()
    assert outcomes[0].result == "pending"
    assert action(ctx, action_id)["status"] == "executing"
    assert contribution(ctx, cid)["state_reason"] == "reconcile_pending"
    again = await Reconciler(ctx, GitHubReader(gh.client())).run_once({action_id})
    assert again[0].result == "interrupted"


async def test_resume_does_not_submit(ctx: AppContext, gh: FakeGitHub) -> None:
    """After reconciliation to SUBMIT_FAILED plus /resume, zero writes until a fresh two-tap approval."""
    from ossbot.gatekeeper.app import housekeeping_once

    cid, action_id = executing(ctx, "open_pr")
    ctx.control.kill("crash test", "tg:4242")
    await Reconciler(ctx, GitHubReader(gh.client())).run_once()
    assert contribution(ctx, cid)["state"] == "SUBMIT_FAILED"
    ctx.control.request_resume("tg:4242", "resume")
    for flag in (ctx.paths.killed_flag, ctx.paths.paused_flag, ctx.paths.resume_request):
        flag.unlink(missing_ok=True)  # what the root enforcer does
    await housekeeping_once(ctx)
    assert ctx.control.view().state == "running"
    await Reconciler(ctx, GitHubReader(gh.client())).run_once()
    assert gh.writes == []
    assert action(ctx, action_id)["status"] == "failed"
    assert contribution(ctx, cid)["state"] == "SUBMIT_FAILED"


def test_github_error_flags() -> None:
    assert GitHubError("x", status=401).auth
    assert not GitHubError("x", status=500, transient=True).auth
