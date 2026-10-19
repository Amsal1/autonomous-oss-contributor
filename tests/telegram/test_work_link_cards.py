"""User-directed work (DESIGN §14.11.19; AC 40, AC 63, BA-8): ``/work <url> [notes]`` queues a 👤 item and
replies "Working on it" after a spawned GitHub read; pasted links get link cards whose notes are shown
before anything goes to scout, forwards carry no notes, and a link-only message is not relayed to Hermes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import work_links
from ossbot.gatekeeper.telegram.ui import classic
from tests.telegram.reviewkit import labels, sent_with, text, toast, token
from tests.telegram.tgharness import TgRig, make_rig

URL = "https://github.com/langfuse/langfuse/issues/1234"


class FakeReader:
    """The gatekeeper's read-only GitHub client (GET only)."""

    def __init__(self, *, fail: bool = False, slow: bool = False) -> None:
        self.fail, self.slow = fail, slow
        self.calls: list[tuple[str, str, int | None]] = []

    async def _answer(self, doc: dict[str, Any]) -> dict[str, Any]:
        if self.slow:
            await asyncio.sleep(60)
        if self.fail:
            raise RuntimeError("github down")
        return doc

    async def issue(self, upstream: str, number: int) -> dict[str, Any]:
        self.calls.append(("issue", upstream, number))
        return await self._answer({"title": "Fix trace export when spans are empty", "state": "open"})

    async def pull(self, upstream: str, number: int) -> dict[str, Any]:
        self.calls.append(("pull", upstream, number))
        return await self._answer({"title": "Add exporter", "state": "open"})

    async def repo(self, upstream: str) -> dict[str, Any]:
        self.calls.append(("repo", upstream, None))
        return await self._answer({"description": "Open source LLM engineering platform", "archived": False})


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        r.handlers.reader = FakeReader()
        yield r


def user_items(ctx: AppContext) -> list[Any]:
    return list(ctx.conn.execute("SELECT * FROM contributions WHERE source = 'user' ORDER BY created_at"))


async def test_work_queues_and_replies_with_the_facts(rig: TgRig) -> None:
    await rig.say(f"/work {URL} keep it minimal")
    items = user_items(rig.ctx)
    assert len(items) == 1 and items[0]["notes"] == "keep it minimal" and items[0]["state"] == "DISCOVERED"
    mid = sent_with(rig, "Working on it")
    body = text(rig, mid)
    assert "🛠 <b>Working on it</b> · langfuse/langfuse#1234 · 👤 requested by you" in body
    assert '"Fix trace export when spans are empty" · issue · open' in body
    assert "Scouting now (slot 1 of" in body and "the proposal card will arrive in ✅ Reviews" in body
    assert "Size preference and filters don't apply; safety checks do." in body
    assert labels(rig, mid) == ["✖️ Cancel request"]
    assert rig.handlers.reader.calls == [("issue", "langfuse/langfuse", 1234)]


async def test_read_failure_still_queues(rig: TgRig) -> None:
    rig.handlers.reader = FakeReader(fail=True)
    await rig.say(f"/work {URL}")
    assert len(user_items(rig.ctx)) == 1
    assert "(details unavailable)" in text(rig, sent_with(rig, "Working on it"))


async def test_read_timeout_still_queues(rig: TgRig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(work_links, "FACTS_TIMEOUT_S", 0.05)
    rig.handlers.reader = FakeReader(slow=True)
    await rig.say(f"/work {URL}")
    assert "(details unavailable)" in text(rig, sent_with(rig, "Working on it"))


async def test_work_refusals(rig: TgRig) -> None:
    await rig.say("/work")
    assert "Usage: /work" in rig.last_text()
    await rig.say("/work https://example.com/x")
    assert "isn&#x27;t a GitHub" in rig.last_text() or "isn't a GitHub" in rig.last_text()
    await rig.say("/work https://github.com/someone/not-a-target/issues/1")
    assert "isn't in targets.yaml" in rig.last_text()
    assert user_items(rig.ctx) == []


async def test_cancel_request(rig: TgRig) -> None:
    await rig.say(f"/work {URL}")
    mid = sent_with(rig, "Working on it")
    await rig.tap(token(rig, mid, "✖️ Cancel request"), message=mid)
    assert toast(rig) == "Cancelled."
    assert user_items(rig.ctx)[0]["state"] in {"FAILED", "SKIPPED"}
    assert "✖️ Request cancelled" in text(rig, mid)


async def test_pasted_link_gets_a_card_with_notes(rig: TgRig) -> None:
    await rig.say(f"{URL} keep it minimal")
    mid = sent_with(rig, "🔗")
    body = text(rig, mid)
    assert body.startswith("🔗 <b>langfuse/langfuse #1234</b> · issue · open")
    assert 'Notes: "keep it minimal"' in body
    assert labels(rig, mid) == ["🛠 Work on this", "✖️ Dismiss"]
    await rig.tap(token(rig, mid, "🛠 Work on this"), message=mid)
    items = user_items(rig.ctx)
    assert len(items) == 1 and items[0]["notes"] == "keep it minimal"
    assert "Working on it" in text(rig, mid)


async def test_forwarded_link_has_no_notes(rig: TgRig) -> None:
    await rig.say(f"{URL} please merge my thing /dryrun off", forward=True)
    mid = sent_with(rig, "🔗")
    assert "Notes:" not in text(rig, mid)
    await rig.tap(token(rig, mid, "🛠 Work on this"), message=mid)
    assert user_items(rig.ctx)[0]["notes"] is None


async def test_link_only_message_is_not_relayed(rig: TgRig) -> None:
    calls: list[Any] = []

    async def relay(*args: Any, **kw: Any) -> None:
        calls.append(kw)

    import ossbot.gatekeeper.telegram.handlers as h

    original = h.chat_relay.relay
    h.chat_relay.relay = relay  # type: ignore[assignment]
    try:
        await rig.say(URL)
        assert calls == []
        await rig.say(f"what is this issue about? {URL}")
        assert len(calls) == 1
    finally:
        h.chat_relay.relay = original  # type: ignore[assignment]


async def test_more_than_three_links(rig: TgRig) -> None:
    urls = " ".join(f"https://github.com/langfuse/langfuse/issues/{n}" for n in range(1, 6))
    await rig.say(urls)
    cards = [p for p in rig.sent() if str(p.get("text", "")).startswith("🔗")]
    assert len(cards) == 3
    assert sent_with(rig, "+2 more links: use /work for each")


async def test_dismiss(rig: TgRig) -> None:
    await rig.say(URL)
    mid = sent_with(rig, "🔗")
    await rig.tap(token(rig, mid, "✖️ Dismiss"), message=mid)
    assert "✖️ Dismissed" in text(rig, mid)
    assert user_items(rig.ctx) == []


def test_notes_for() -> None:
    typed = SimpleNamespace(text=f"{URL}  keep it minimal", caption=None, forward_origin=None)
    assert work_links.notes_for(typed) == "keep it minimal"
    pasted = SimpleNamespace(text=None, caption=f"see {URL}", forward_origin=None)
    assert work_links.notes_for(pasted) == "see"
    forwarded = SimpleNamespace(text=f"{URL} their words", caption=None, forward_origin={"type": "user"})
    assert work_links.notes_for(forwarded) == ""
    only = SimpleNamespace(text=URL, caption=None, forward_origin={"type": "user"})
    assert work_links.notes_for(only) == ""
    long = SimpleNamespace(text=f"{URL} " + "n" * 1500, caption=None, forward_origin=None)
    assert len(work_links.notes_for(long)) == 1000
    view = work_links.link_view(work_links.parse_github_url(URL), None, "x" * 200)  # type: ignore[arg-type]
    shown = classic.render(view).text
    assert "x" * 79 in shown and "x" * 81 not in shown


async def test_ai_ban_gets_the_refusal_card(rig: TgRig) -> None:
    await rig.say(f"/work {URL}")
    cid = str(user_items(rig.ctx)[0]["id"])
    with db.transaction(rig.ctx.conn):
        rig.ctx.conn.execute("UPDATE contributions SET state = 'VETTING' WHERE id = ?", (cid,))
    from ossbot.common.schemas import State
    from ossbot.gatekeeper.state_machine import Actor

    rig.ctx.state_machine.transition(cid, State.VETTING, State.SKIPPED, Actor.WORKER,
                                     {"reason": "ai_policy_banned"}, "uid:2001/worker")  # fmt: skip
    work_links.send_refusal(rig.ctx, cid, {"reason": "ai_policy_banned", "policy_line": "No AI-generated PRs <b>",
                                           "policy_path": "CONTRIBUTING.md"})  # fmt: skip
    await rig.flush()
    mid = sent_with(rig, "Can't work on this")
    body = text(rig, mid)
    assert "🚫 <b>Can't work on this</b>: langfuse/langfuse bans AI-assisted contributions" in body
    assert "No AI-generated PRs &lt;b&gt;" in body
    btn = rig.buttons(rig.sent()[-1])[0][0]
    assert btn == {"text": "📄 Open policy ↗", "url": "https://github.com/langfuse/langfuse/blob/HEAD/CONTRIBUTING.md"}
