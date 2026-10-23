"""S10/S11 live status card (DESIGN §14.11.11, §14.19 item 2): a reply to the card in ✅ Reviews, edited in
place at most once per 60 s and only on change, P1 events as replies, merged/closed finalization, the
timeline and Stop watching (M4)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest

from ossbot.common.clock import FakeClock
from ossbot.common.schemas import State
from ossbot.gatekeeper import db, owner_fix
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.github_read import GitHubError
from ossbot.gatekeeper.telegram import bot_finding_cards, status_card
from ossbot.gatekeeper.telegram.prefs import UxPrefs
from ossbot.gatekeeper.telegram.ui import blocks, classic, rich
from tests.conftest import insert_contribution, write_flag
from tests.telegram import golden
from tests.telegram.reviewkit import labels, sent_with, text, toast, token
from tests.telegram.tgharness import TgRig, make_rig


class Reader:
    def __init__(self) -> None:
        self.pr: dict[str, Any] = {"state": "open", "title": "fix(web): handle empty spans", "html_url":
                                   "https://github.com/o/r/pull/5678", "head": {"sha": "a" * 40}}  # fmt: skip
        self.checks: list[dict[str, Any]] = [{"name": "build", "status": "completed", "conclusion": "success"},
                                             {"name": "e2e", "status": "in_progress"}]  # fmt: skip
        self.review_list: list[dict[str, Any]] = []
        self.comments: list[dict[str, Any]] = []
        self.inline: list[dict[str, Any]] = []  # inline review comments (review-bot findings)
        self.refetch: dict[int, dict[str, Any]] = {}  # GET /pulls/comments/{id} (the Fix-this re-fetch)
        self.refetched: list[int] = []

    async def pull(self, upstream: str, number: int) -> dict[str, Any]:
        return self.pr

    async def review_comments(self, upstream: str, number: int) -> list[dict[str, Any]]:
        return self.inline

    async def review_comment(self, upstream: str, comment_id: int) -> dict[str, Any] | None:
        self.refetched.append(comment_id)
        return self.refetch.get(comment_id)

    async def check_runs(self, upstream: str, sha: str) -> list[dict[str, Any]]:
        return self.checks

    async def reviews(self, upstream: str, number: int) -> list[dict[str, Any]]:
        return self.review_list

    async def comments_since(self, upstream: str, number: int, since: str) -> list[dict[str, Any]]:
        return self.comments


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def _pr(ctx: AppContext) -> str:
    return insert_contribution(ctx, State.WATCHING, pr_number=5678, pr_url="https://github.com/o/r/pull/5678")


def _status(rig: TgRig) -> Any:
    return db.one(rig.ctx.conn, "SELECT * FROM tg_views WHERE kind = 'status'")


async def test_card_is_sent_then_edited_only_on_change_and_at_most_once_a_minute(rig: TgRig, clock: FakeClock
                                                                                  ) -> None:  # fmt: skip
    cid, reader = _pr(rig.ctx), Reader()
    assert await status_card.refresh(rig.ctx, reader, cid)
    await rig.flush()
    view = _status(rig)
    assert view is not None
    doc = json.dumps(rig.req.calls[-1][1], ensure_ascii=False)
    assert '"PR ", {"type": "code", "text": "#5678"}, " open' in doc  # the PR number one tap to copy
    assert "CI: ✅ 1 · ⏳ 1 · ❌ 0" in doc and "🔄 Refresh" in doc
    await status_card.refresh(rig.ctx, reader, cid)  # first poll after the send records the hash
    await rig.flush()
    edits = len([m for m, _ in rig.req.calls if m == "editMessageText"])
    await status_card.refresh(rig.ctx, reader, cid)  # unchanged: no edit
    await rig.flush()
    assert len([m for m, _ in rig.req.calls if m == "editMessageText"]) == edits
    reader.checks[1] = {"name": "e2e", "status": "completed", "conclusion": "success"}
    clock.advance(timedelta(seconds=30))
    await status_card.refresh(rig.ctx, reader, cid)  # changed but within 60 s: wait
    await rig.flush()
    assert len([m for m, _ in rig.req.calls if m == "editMessageText"]) == edits
    clock.advance(timedelta(seconds=31))
    await status_card.refresh(rig.ctx, reader, cid)
    await rig.flush()
    assert "CI: ✅ 2 · ⏳ 0" in json.dumps(rig.req.last("editMessageText"), ensure_ascii=False)


class CountingReader(Reader):
    """Counts PR reads; ``error`` makes every read raise it (a 403, a rate limit)."""

    def __init__(self, error: Exception | None = None) -> None:
        super().__init__()
        self.calls = 0
        self.error = error

    async def pull(self, upstream: str, number: int) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.pr


@pytest.fixture(autouse=True)
def _poll_gate() -> None:
    status_card.reset_poll_gate()


def _state(rig: TgRig) -> dict[str, Any]:
    row = _status(rig)
    assert row is not None
    return dict(json.loads(row["state_json"] or "{}"))


def _contribution_state(rig: TgRig, cid: str) -> str:
    row = db.one(rig.ctx.conn, "SELECT state FROM contributions WHERE id = ?", (cid,))
    assert row is not None
    return str(row["state"])


async def test_tick_does_not_repoll_after_the_edit_is_delivered(rig: TgRig, clock: FakeClock) -> None:
    """The 2026-10-22 loop: the delivered edit used to wipe ``polled_at``/``hash``, so every tick re-polled."""
    cid, reader = _pr(rig.ctx), CountingReader()
    assert await status_card.tick(rig.ctx, reader) == 1  # the card is sent
    await rig.flush()
    clock.advance(timedelta(seconds=301))
    await status_card.tick(rig.ctx, reader)  # first poll with a card: hash recorded, the card edited
    await rig.flush()  # the edit is delivered (mark_sent writes the view row)
    assert reader.calls == 2 and rig.req.last("editMessageText") is not None
    state = _state(rig)
    assert {"polled_at", "hash", "edited_at"} <= set(state)
    await status_card.tick(rig.ctx, reader)
    clock.advance(timedelta(seconds=299))
    await status_card.tick(rig.ctx, reader)
    await rig.flush()
    assert reader.calls == 2
    clock.advance(timedelta(seconds=2))
    await status_card.tick(rig.ctx, reader)
    assert reader.calls == 3
    assert _contribution_state(rig, cid) == "WATCHING"


async def test_a_raising_reader_is_not_repolled_at_once(rig: TgRig, clock: FakeClock) -> None:
    cid, reader = _pr(rig.ctx), CountingReader(GitHubError("GET /pulls: HTTP 403", status=403))
    await status_card.tick(rig.ctx, reader)
    assert reader.calls == 1
    await status_card.tick(rig.ctx, reader)
    clock.advance(timedelta(seconds=299))
    await status_card.tick(rig.ctx, reader)
    assert reader.calls == 1
    clock.advance(timedelta(seconds=2))
    await status_card.tick(rig.ctx, reader)
    assert reader.calls == 2
    clock.advance(timedelta(seconds=599))  # the second failure in a row waits 600 s
    await status_card.tick(rig.ctx, reader)
    assert reader.calls == 2
    clock.advance(timedelta(seconds=2))
    await status_card.tick(rig.ctx, reader)
    assert reader.calls == 3
    assert _contribution_state(rig, cid) == "WATCHING"


async def test_rate_limit_reset_is_honoured(rig: TgRig, clock: FakeClock) -> None:
    _pr(rig.ctx)
    reset = clock.now().timestamp() + 1800
    reader = CountingReader(GitHubError("GET /pulls: HTTP 403", status=403, reset_epoch=reset))
    await status_card.tick(rig.ctx, reader)
    clock.advance(timedelta(seconds=301))
    await status_card.tick(rig.ctx, reader)
    clock.advance(timedelta(seconds=1498))
    await status_card.tick(rig.ctx, reader)
    assert reader.calls == 1
    clock.advance(timedelta(seconds=2))
    await status_card.tick(rig.ctx, reader)
    assert reader.calls == 2


async def test_failure_backoff_survives_a_restart(rig: TgRig, clock: FakeClock) -> None:
    """With a status row, ``poll_after`` and ``poll_failures`` are persisted: a restarted gatekeeper (an
    empty memory gate) still waits, and a success clears them."""
    cid, reader = _pr(rig.ctx), CountingReader()
    await status_card.tick(rig.ctx, reader)
    await rig.flush()
    clock.advance(timedelta(seconds=301))
    reader.error = GitHubError("GET /pulls: HTTP 403", status=403)
    await status_card.tick(rig.ctx, reader)
    assert reader.calls == 2 and _state(rig)["poll_failures"] == 1
    status_card.reset_poll_gate()
    clock.advance(timedelta(seconds=299))
    await status_card.tick(rig.ctx, reader)
    assert reader.calls == 2
    clock.advance(timedelta(seconds=2))
    await status_card.tick(rig.ctx, reader)  # second failure, counted from the persisted value
    assert reader.calls == 3 and _state(rig)["poll_failures"] == 2
    clock.advance(timedelta(seconds=601))
    reader.error = None
    await status_card.tick(rig.ctx, reader)
    await rig.flush()
    state = _state(rig)
    assert reader.calls == 4 and "poll_after" not in state and "poll_failures" not in state
    assert _contribution_state(rig, cid) == "WATCHING"


async def test_timeline_edit_keeps_the_poll_bookkeeping(rig: TgRig, clock: FakeClock) -> None:
    _pr(rig.ctx)
    reader = CountingReader()
    await status_card.tick(rig.ctx, reader)
    await rig.flush()
    clock.advance(timedelta(seconds=301))
    await status_card.tick(rig.ctx, reader)
    await rig.flush()
    mid = int(_status(rig)["message_id"])
    await rig.tap(rig.token_of(rig.req.last("editMessageText"), "📜 Timeline"), message=mid)
    await rig.flush()
    assert "Timeline" in json.dumps(rig.req.last("editMessageText"), ensure_ascii=False)
    state = _state(rig)
    assert {"events", "polled_at", "hash", "seen"} <= set(state)


@pytest.mark.parametrize("gating", ["approval_required", "label_or_comment", "auto", None])
async def test_no_checks_on_a_gated_repo_waits_for_approval(rig: TgRig, gating: str | None) -> None:
    """Post-v1 (§9.12, §14.11.11): no checks yet on a repo whose CI starts after a maintainer approves it
    (or adds a label) is neither passing nor failing."""
    vetting = {"ci_on_pr": "yes", "ci_gating": gating} if gating else {}
    cid = insert_contribution(rig.ctx, State.WATCHING, pr_number=5678, pr_url="https://github.com/o/r/pull/5678",
                              vetting_json=json.dumps(vetting))  # fmt: skip
    reader = Reader()
    reader.checks = []
    assert await status_card.refresh(rig.ctx, reader, cid)
    await rig.flush()
    doc = json.dumps(rig.req.calls[-1][1], ensure_ascii=False)
    waits = gating in {"approval_required", "label_or_comment"}
    assert ("⏳ CI waits for maintainer approval" in doc) is waits
    assert ("CI: ✅ 0 · ⏳ 0 · ❌ 0" in doc) is not waits


async def test_p1_events_reply_to_the_status_card_once(rig: TgRig) -> None:
    cid, reader = _pr(rig.ctx), Reader()
    await status_card.refresh(rig.ctx, reader, cid)
    await rig.flush()
    mid = int(_status(rig)["message_id"])
    reader.review_list = [{"id": 9, "user": {"login": "maintainer"}, "state": "CHANGES_REQUESTED",
                           "submitted_at": "2026-10-19T12:00:00Z"}]  # fmt: skip
    reader.checks.append({"name": "lint", "status": "completed", "conclusion": "failure"})
    for _ in range(2):
        await status_card.refresh(rig.ctx, reader, cid)
        await rig.flush()
    replies = [p for m, p in rig.req.calls if m in {"sendMessage", "sendRichMessage"}
               and (p.get("reply_parameters") or {}).get("message_id") == mid]  # fmt: skip
    texts = [json.dumps(p, ensure_ascii=False) for p in replies]
    assert len(replies) == 2 and any("👀 Review" in t for t in texts) and any("❌ Check failed" in t for t in texts)


async def test_merged_finalizes_with_only_the_link_and_a_summary(rig: TgRig, clock: FakeClock) -> None:
    cid, reader = _pr(rig.ctx), Reader()
    await status_card.refresh(rig.ctx, reader, cid)
    await rig.flush()
    reader.pr.update({"merged": True, "merged_by": {"login": "boss"}, "merged_at": "2026-10-19T12:00:00Z",
                      "created_at": "2026-10-17T12:00:00Z", "additions": 10, "deletions": 2})  # fmt: skip
    await status_card.refresh(rig.ctx, reader, cid)
    await rig.flush()
    edit = json.dumps(rig.req.last("editMessageText"), ensure_ascii=False)
    assert "🎉" in edit and "Merged" in edit and "boss" in edit and "🔄 Refresh" not in edit
    summary = json.dumps(rig.sent()[-1] if rig.sent() else rig.sent("sendRichMessage")[-1], ensure_ascii=False)
    assert "Time to merge 2 d 0 h" in summary and "12 lines changed" in summary


@pytest.mark.parametrize(
    ("state", "cols", "shown"),
    [
        (State.WATCHING, {}, True),
        (State.SUBMITTED, {}, False),
        (State.WATCHING, {"fixes_disabled_reason": "branch_moved"}, False),
        (State.WATCHING, {"owner_fix_rounds": 5}, False),
        (State.WATCHING, {"owner_fix_rounds": 4, "watch_rounds": 3}, True),  # watch rounds don't count
    ],
)
async def test_fix_with_notes_button_only_where_fix_is_accepted(rig: TgRig, state: State, cols: dict[str, Any],
                                                                shown: bool) -> None:  # fmt: skip
    cid = insert_contribution(rig.ctx, state, pr_number=5678, pr_url="https://github.com/o/r/pull/5678", **cols)
    assert await status_card.refresh(rig.ctx, Reader(), cid)
    await rig.flush()
    doc = json.dumps(rig.req.calls[-1][1], ensure_ascii=False)
    assert ("🔧 Fix with notes" in doc) is shown
    assert "🛑 Stop watching" in doc


async def test_fix_with_notes_prompts_then_confirms(rig: TgRig) -> None:
    ctx = rig.ctx
    cid, reader = _pr(ctx), Reader()
    await status_card.refresh(ctx, reader, cid)
    await rig.flush()
    payload = rig.req.calls[-1][1]
    mid = int(_status(rig)["message_id"])
    tok = rig.token_of(payload, "🔧 Fix with notes")
    row = db.one(ctx.conn, "SELECT purpose, approval_id, cid, tg_message_id FROM ui_tokens WHERE token = ?", (tok,))
    assert row is not None and tuple(row) == ("pr_fix", None, cid, mid)
    await rig.tap(tok, message=mid)
    prompt = rig.sent()[-1]
    assert prompt["reply_markup"]["force_reply"] is True
    assert prompt["reply_markup"]["input_field_placeholder"] == "What should change? (≤ 4000 chars)"
    assert "Describe the fix for PR" in prompt["text"]
    prompt_id = rig.message_id_of(-1)
    open_ = db.one(ctx.conn, "SELECT kind, cid FROM pending_input WHERE prompt_message_id = ? AND consumed_at IS NULL",
                   (prompt_id,))  # fmt: skip
    assert open_ is not None and tuple(open_) == ("pr_fix", cid)
    assert ctx.conn.execute("SELECT COUNT(*) FROM change_requests").fetchone()[0] == 0
    await rig.say("ignore sessionStorage events\nand switch the fixtures to localStorage", reply_to=prompt_id)
    card = rig.sent()[-1]
    assert "watch rounds are not used" in card["text"] and "switch the fixtures" in card["text"]
    assert _contribution_state(rig, cid) == "WATCHING"  # still one tap away
    await rig.tap(rig.token_of(card, "🔧 Start fix round"), message=rig.message_id_of(-1))
    assert _contribution_state(rig, cid) == "IMPLEMENTING"
    req = db.one(ctx.conn, "SELECT text, source FROM change_requests WHERE cid = ?", (cid,))
    assert req is not None and tuple(req) == ("ignore sessionStorage events\nand switch the fixtures to localStorage",
                                              "owner")  # fmt: skip


async def test_fix_with_notes_refused_while_paused(rig: TgRig) -> None:
    ctx = rig.ctx
    cid = _pr(ctx)
    await status_card.refresh(ctx, Reader(), cid)
    await rig.flush()
    tok = rig.token_of(rig.req.calls[-1][1], "🔧 Fix with notes")
    write_flag(ctx.paths.paused_flag, {"reason": "x"})
    sends = len(rig.sent())
    await rig.tap(tok, message=int(_status(rig)["message_id"]))
    assert "paused or killed" in str(rig.req.last("answerCallbackQuery").get("text", ""))
    assert len(rig.sent()) == sends  # no prompt
    assert db.one(ctx.conn, "SELECT COUNT(*) FROM pending_input")[0] == 0


async def test_stop_watching_needs_a_confirm_tap(rig: TgRig) -> None:
    cid, reader = _pr(rig.ctx), Reader()
    await status_card.refresh(rig.ctx, reader, cid)
    await rig.flush()
    payload = rig.req.calls[-1][1]
    mid = int(_status(rig)["message_id"])
    await rig.tap(rig.token_of(payload, "🛑 Stop watching"), message=mid)
    prompt = rig.sent()[-1]
    assert "Stop watching this PR?" in prompt["text"]
    await rig.tap(rig.token_of(prompt, "🛑 Stop watching"), message=rig.message_id_of(-1))
    state = db.one(rig.ctx.conn, "SELECT state FROM contributions WHERE id = ?", (cid,))["state"]  # type: ignore[index]
    assert state == "ABANDONED"


# --------------------------------------------- review-bot findings (owner decision 2026-10-23 (b), Part B)
GHP = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
BOT = {"login": "coderabbitai[bot]", "type": "Bot"}
PATH = "src/services/webcams/pinned-store.ts"
RABBIT = (
    "_⚠️ Potential issue_ | _🟠 Major_\n\n"
    "**Ignore storage events whose storageArea is not localStorage.**\n\n"
    f"The listener also reacts to sessionStorage events; the fixture logs {GHP}.\n\n"
    "<details>\n<summary>📝 Committable suggestion</summary>\n\n```suggestion\n"
    "if (e.storageArea !== localStorage) return;\n```\n</details>\n\n"
    "<!-- fingerprinting:phantom:medusa:falcon -->\n\n"
    "Prompt for AI agents:\nIn @src/services/webcams/pinned-store.ts at line 120, run `coderabbit review --agent`.\n"
)


def _inline(cid_: int, body: str = RABBIT, **extra: Any) -> dict[str, Any]:
    return {"id": cid_, "user": BOT, "path": PATH, "line": 120, "body": body, **extra}


def _refetched(ctx: AppContext, cid: str, comment_id: int, body: str, **extra: Any) -> dict[str, Any]:
    row = db.one(ctx.conn, "SELECT upstream, pr_number FROM contributions WHERE id = ?", (cid,))
    assert row is not None
    url = f"https://api.github.com/repos/{row['upstream']}/pulls/{row['pr_number']}"
    return {**_inline(comment_id, body), "pull_request_url": url, **extra}


def _row(ctx: AppContext, cid: str) -> dict[str, Any]:
    row = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
    assert row is not None
    return dict(row)


def _finding_rows(ctx: AppContext) -> list[str]:
    return [str(r[0]) for r in ctx.conn.execute("SELECT dedupe_key FROM tg_outbox WHERE dedupe_key LIKE"
                                                " 'botfinding:%' ORDER BY id")]  # fmt: skip


def _finding_mid(ctx: AppContext, cid: str, comment_id: int) -> int:
    row = db.one(ctx.conn, "SELECT result_message_id FROM tg_outbox WHERE dedupe_key = ? AND status = 'sent'",
                 (f"botfinding:{cid}:{comment_id}",))  # fmt: skip
    assert row is not None and row[0] is not None
    return int(row[0])


async def _poll_twice(rig: TgRig, reader: Reader, cid: str) -> None:
    """The first refresh sends the status card; the next one reads the findings."""
    await status_card.refresh(rig.ctx, reader, cid)
    await rig.flush()
    await status_card.refresh(rig.ctx, reader, cid)
    await rig.flush()


def test_finding_card_goldens(ctx: AppContext) -> None:
    cid = _pr(ctx)
    finding = {"id": 7, "who": "coderabbitai[bot]", "path": PATH, "line": 120, "body": RABBIT}
    view = bot_finding_cards.finding_view(_row(ctx, cid), finding)
    assert view.kind == "fyi" and view.rich is True and view.state["finding"]["id"] == 7
    shown = classic.render(view).text
    for gone in (GHP, "Committable suggestion", "storageArea !== localStorage", "fingerprinting", "Prompt for AI",
                 "coderabbit review"):  # fmt: skip
        assert gone not in shown, gone
    assert "Ignore storage events whose storageArea" in shown and f"<code>{cid}</code>" in shown
    keys = "\n".join(" | ".join(r) for r in view.keyboard_labels())
    golden.check("bot_finding.html", shown.replace(cid, "c_CID") + "\n--\n" + keys + "\n")
    dumped = json.dumps(blocks.dump(rich.render(view)), ensure_ascii=False, indent=1, sort_keys=True)
    assert GHP not in dumped and "Committable suggestion" not in dumped
    golden.check("bot_finding.json", dumped.replace(cid, "c_CID") + "\n")
    assert view.rows[0][2].url == "https://github.com/o/r/pull/5678#discussion_r7"  # built by the gatekeeper


def test_finding_body_is_cut_at_300(ctx: AppContext) -> None:
    cid = _pr(ctx)
    view = bot_finding_cards.finding_view(_row(ctx, cid), {"id": 7, "who": "x[bot]", "path": "a", "body": "y" * 5000})
    shown = classic.render(view).text
    assert "y" * 301 not in shown and "y" * 250 in shown
    assert "a:?" in shown  # no line from GitHub


def test_display_body_keeps_raw_text_elsewhere() -> None:
    assert bot_finding_cards.display_body("a<details><summary>x</summary><details>in</details>y</details>b") == "ab"
    assert bot_finding_cards.display_body("keep\n<!-- hidden -->\nPROMPT FOR AI AGENTS\nrun it") == "keep"
    assert bot_finding_cards.display_body("<details>open") == "<details>open"  # an unclosed block stays visible


async def test_one_card_per_bot_inline_comment(rig: TgRig) -> None:
    ctx = rig.ctx
    cid, reader = _pr(ctx), Reader()
    reader.inline = [_inline(7), _inline(8, "a reply", in_reply_to_id=7),
                     {"id": 9, "user": {"login": "maintainer", "type": "User"}, "path": "a.ts", "line": 1,
                      "body": "a maintainer's inline comment"}]  # fmt: skip
    await _poll_twice(rig, reader, cid)
    await status_card.refresh(ctx, reader, cid)  # a third poll: still one card (dedupe per comment id)
    await rig.flush()
    assert _finding_rows(ctx) == [f"botfinding:{cid}:7"]
    mid = _finding_mid(ctx, cid, 7)
    status_mid = int(_status(rig)["message_id"])
    sent = db.one(ctx.conn, "SELECT payload_json FROM tg_outbox WHERE dedupe_key = ?", (f"botfinding:{cid}:7",))
    assert sent is not None
    payload = json.loads(sent[0])
    reply = payload.get("reply_parameters") or payload["_classic"]["reply_parameters"]
    assert reply["message_id"] == status_mid
    tokens = ctx.conn.execute("SELECT purpose, approval_id, single_use, arg FROM ui_tokens WHERE tg_message_id = ?"
                              " ORDER BY purpose", (mid,)).fetchall()  # fmt: skip
    assert [(t[0], t[1], t[2]) for t in tokens] == [("pr_bot_fix", None, 1), ("pr_bot_ignore", None, 1)]
    assert json.loads(tokens[0][3]) == {"cid": cid, "comment_id": 7}


async def test_at_most_five_finding_cards_per_poll(rig: TgRig) -> None:
    ctx = rig.ctx
    cid, reader = _pr(ctx), Reader()
    reader.inline = [_inline(n, f"finding {n}") for n in range(1, 8)]
    await _poll_twice(rig, reader, cid)
    assert len(_finding_rows(ctx)) == status_card.FINDINGS_PER_POLL == 5
    await status_card.refresh(ctx, reader, cid)
    await rig.flush()
    assert sorted(_finding_rows(ctx)) == sorted(f"botfinding:{cid}:{n}" for n in range(1, 8))


async def test_bots_are_not_maintainers_and_never_p1(rig: TgRig) -> None:
    ctx = rig.ctx
    cid, reader = _pr(ctx), Reader()
    await status_card.refresh(ctx, reader, cid)
    await rig.flush()
    mid = int(_status(rig)["message_id"])
    login = ctx.config.profile.github_login
    reader.review_list = [{"id": 10, "user": BOT, "state": "CHANGES_REQUESTED", "submitted_at": "2026-10-23T10:00:00Z"}]
    reader.comments = [{"id": 11, "user": BOT, "body": f"@{login} walkthrough", "created_at": "2026-10-23T10:01:00Z"}]
    await status_card.refresh(ctx, reader, cid)
    await rig.flush()
    loud = [p for m, p in rig.req.calls if m in {"sendMessage", "sendRichMessage"}
            and (p.get("reply_parameters") or {}).get("message_id") == mid]  # fmt: skip
    assert loud == []
    facts = await status_card.collect(reader, _row(ctx, cid), login)
    assert facts.maintainer_comments == 0 and facts.changes_by == [] and facts.approvals == 0
    assert {e["id"] for e in facts.events} >= {"review:10", "comment:11"}  # still in the timeline
    reader.review_list.append({"id": 12, "user": {"login": "maintainer"}, "state": "CHANGES_REQUESTED",
                               "submitted_at": "2026-10-23T10:05:00Z"})  # fmt: skip
    await status_card.refresh(ctx, reader, cid)
    await rig.flush()
    loud = [json.dumps(p, ensure_ascii=False) for m, p in rig.req.calls if m in {"sendMessage", "sendRichMessage"}
            and (p.get("reply_parameters") or {}).get("message_id") == mid]  # fmt: skip
    assert len(loud) == 1 and "👀 Review" in loud[0] and "maintainer" in loud[0]


async def _classic_card(rig: TgRig, reader: Reader, cid: str, comment_id: int = 7) -> int:
    UxPrefs(rig.ctx.conn, rig.ctx.clock).set("rich_views", "off")
    rig.handlers.reader = reader
    await _poll_twice(rig, reader, cid)
    return _finding_mid(rig.ctx, cid, comment_id)


def _requests(ctx: AppContext, cid: str) -> list[Any]:
    return list(ctx.conn.execute("SELECT * FROM change_requests WHERE cid = ?", (cid,)).fetchall())


async def test_fix_this_refetches_then_starts_an_owner_round(rig: TgRig) -> None:
    ctx = rig.ctx
    cid, reader = _pr(ctx), Reader()
    reader.inline = [_inline(7, "the card's copy")]
    reader.refetch[7] = _refetched(ctx, cid, 7, "the re-fetched body")
    mid = await _classic_card(rig, reader, cid)
    await rig.tap(token(rig, mid, "🔧 Fix this"), message=mid)
    confirm = sent_with(rig, "Fix this finding?")
    assert "Round 1 of 5" in text(rig, confirm) and labels(rig, confirm) == ["🔧 Start fix round", "↩️ Back"]
    assert reader.refetched == [] and _requests(ctx, cid) == [] and _row(ctx, cid)["state"] == "WATCHING"
    start = token(rig, confirm, "🔧 Start fix round")
    await rig.tap(start, message=confirm)
    assert reader.refetched == [7]
    (req,) = _requests(ctx, cid)
    assert (req["source"], req["text"], req["untrusted_text"]) == ("review_bot", owner_fix.BOT_INSTRUCTION,
                                                                     "the re-fetched body")  # fmt: skip
    assert json.loads(req["source_ref"]) == {"comment_id": 7, "path": PATH, "line": 120, "author": "coderabbitai[bot]"}
    c = _row(ctx, cid)
    assert (c["state"], c["state_reason"], c["owner_fix_rounds"], c["watch_rounds"]) == (
        "IMPLEMENTING", "owner_fix_round", 1, 0)  # fmt: skip
    assert "Fix round 1/5 started" in text(rig, confirm) and labels(rig, confirm) == []
    assert "Fix round 1/5 started" in text(rig, mid) and labels(rig, mid) == []
    await rig.tap(start, message=confirm)  # single use: a replay does nothing
    assert len(_requests(ctx, cid)) == 1 and reader.refetched == [7]


@pytest.mark.parametrize("case", ["gone", "other_pr", "human", "empty", "not_watching", "capped", "disabled",
                                  "paused"])  # fmt: skip
async def test_fix_this_refusals_store_nothing(rig: TgRig, case: str) -> None:
    ctx = rig.ctx
    cid, reader = _pr(ctx), Reader()
    reader.inline = [_inline(7)]
    reader.refetch[7] = _refetched(ctx, cid, 7, "body")
    mid = await _classic_card(rig, reader, cid)
    await rig.tap(token(rig, mid, "🔧 Fix this"), message=mid)
    confirm = sent_with(rig, "Fix this finding?")
    if case == "gone":
        reader.refetch.clear()
    elif case == "other_pr":
        reader.refetch[7]["pull_request_url"] = "https://api.github.com/repos/langfuse/langfuse/pulls/1"
    elif case == "human":
        reader.refetch[7]["user"] = {"login": "maintainer", "type": "User"}
    elif case == "empty":
        reader.refetch[7]["body"] = "  "
    elif case == "paused":
        write_flag(ctx.paths.paused_flag, {"reason": "x"})
    else:
        column = {"not_watching": "state = 'SUBMITTED'", "capped": "owner_fix_rounds = 5",
                  "disabled": "fixes_disabled_reason = 'branch_moved'"}[case]  # fmt: skip
        with db.transaction(ctx.conn):
            ctx.conn.execute(f"UPDATE contributions SET {column} WHERE id = ?", (cid,))
    before = _row(ctx, cid)["state"]
    await rig.tap(token(rig, confirm, "🔧 Start fix round"), message=confirm)
    assert "Not started:" in text(rig, confirm)
    assert _requests(ctx, cid) == [] and _row(ctx, cid)["state"] == before
    assert _row(ctx, cid)["owner_fix_rounds"] == (5 if case == "capped" else 0)


async def test_fix_this_refused_at_the_first_tap_while_paused(rig: TgRig) -> None:
    ctx = rig.ctx
    cid, reader = _pr(ctx), Reader()
    reader.inline = [_inline(7)]
    mid = await _classic_card(rig, reader, cid)
    write_flag(ctx.paths.paused_flag, {"reason": "x"})
    await rig.tap(token(rig, mid, "🔧 Fix this"), message=mid)
    assert "paused or killed" in toast(rig)
    with pytest.raises(AssertionError):
        sent_with(rig, "Fix this finding?")
    assert _requests(ctx, cid) == []


async def test_back_closes_the_confirm_and_rearms_the_card(rig: TgRig) -> None:
    ctx = rig.ctx
    cid, reader = _pr(ctx), Reader()
    reader.inline = [_inline(7)]
    mid = await _classic_card(rig, reader, cid)
    first_fix, first_ignore = token(rig, mid, "🔧 Fix this"), token(rig, mid, "🙈 Ignore")
    await rig.tap(first_fix, message=mid)
    confirm = sent_with(rig, "Fix this finding?")
    start = token(rig, confirm, "🔧 Start fix round")
    await rig.tap(token(rig, confirm, "↩️ Back"), message=confirm)
    await rig.flush()
    assert "Not started." in text(rig, confirm) and reader.refetched == []
    fresh = token(rig, mid, "🔧 Fix this")
    assert fresh != first_fix and token(rig, mid, "🙈 Ignore") != first_ignore
    await rig.tap(start, message=confirm)  # Back retired its sibling
    await rig.tap(first_ignore, message=mid)  # the re-render retired the card's old buttons
    assert _requests(ctx, cid) == [] and _row(ctx, cid)["state"] == "WATCHING"
    assert ctx.conn.execute("SELECT COUNT(*) FROM audit_events WHERE event = 'bot_finding_ignored'").fetchone()[0] == 0


async def test_ignore_edits_and_audits_only(rig: TgRig) -> None:
    ctx = rig.ctx
    cid, reader = _pr(ctx), Reader()
    reader.inline = [_inline(7)]
    mid = await _classic_card(rig, reader, cid)
    fix_tok = token(rig, mid, "🔧 Fix this")
    await rig.tap(token(rig, mid, "🙈 Ignore"), message=mid)
    await rig.flush()
    assert "🙈 Ignored" in text(rig, mid) and labels(rig, mid) == []
    audit = db.one(ctx.conn, "SELECT data_json, cid FROM audit_events WHERE event = 'bot_finding_ignored'")
    assert audit is not None and json.loads(audit[0]) == {"comment_id": 7} and audit[1] == cid
    await rig.tap(fix_tok, message=mid)  # the card's Fix this died with the Ignore
    with pytest.raises(AssertionError):
        sent_with(rig, "Fix this finding?")
    assert _requests(ctx, cid) == [] and _row(ctx, cid)["state"] == "WATCHING" and reader.refetched == []
