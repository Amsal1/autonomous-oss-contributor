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
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.github_read import GitHubError
from ossbot.gatekeeper.telegram import status_card
from tests.conftest import insert_contribution
from tests.telegram.tgharness import TgRig, make_rig


class Reader:
    def __init__(self) -> None:
        self.pr: dict[str, Any] = {"state": "open", "title": "fix(web): handle empty spans", "html_url":
                                   "https://github.com/o/r/pull/5678", "head": {"sha": "a" * 40}}  # fmt: skip
        self.checks: list[dict[str, Any]] = [{"name": "build", "status": "completed", "conclusion": "success"},
                                             {"name": "e2e", "status": "in_progress"}]  # fmt: skip
        self.review_list: list[dict[str, Any]] = []
        self.comments: list[dict[str, Any]] = []

    async def pull(self, upstream: str, number: int) -> dict[str, Any]:
        return self.pr

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
