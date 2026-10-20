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
    assert "PR #5678 open" in doc and "CI: ✅ 1 · ⏳ 1 · ❌ 0" in doc and "🔄 Refresh" in doc
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
