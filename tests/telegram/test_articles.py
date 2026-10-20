"""Telegraph articles in Telegram (DESIGN §14.12.8; AC 27, AC 65, AC 68) against a fake Telegraph API."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper import telegraph as tgph
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.handlers import KILL_CANCELS
from telegram import Update
from tests.fakes.fake_telegram import callback_update
from tests.telegram.chatkit import FakeBackend, deltas, wire
from tests.telegram.telegraphkit import LONG, FakeTelegraph, enable
from tests.telegram.tgharness import OWNER, TgRig, make_rig

ARTICLE = "📖 Open as article"


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def _labels(payload: dict[str, Any], rig: TgRig) -> list[str]:
    return [str(b.get("text")) for row in rig.buttons(payload) for b in row]


async def _answer(rig: TgRig, text: str = LONG, tools: tuple[str, ...] = ()) -> tuple[dict[str, Any], int]:
    wire(rig.ctx, FakeBackend(deltas(text, tools=tools)))
    await rig.say("explain terraform state")
    final = rig.sent("sendRichMessage")[-1]
    return final, rig.message_id_of(-1, "sendRichMessage")


async def _panel(rig: TgRig, final: dict[str, Any], answer_mid: int) -> tuple[dict[str, Any], int]:
    await rig.tap(rig.token_of(final, ARTICLE), message=answer_mid)
    panel = rig.sent("sendMessage")[-1]
    return panel, rig.message_id_of(-1)


async def test_disabled_means_no_button(rig: TgRig) -> None:
    fake = FakeTelegraph()
    enable(rig.ctx, fake, on=False)
    final, _ = await _answer(rig)
    assert ARTICLE not in _labels(final, rig)


async def test_two_taps_publish_with_allowlisted_nodes_and_a_random_suffix(rig: TgRig) -> None:
    fake = FakeTelegraph()
    enable(rig.ctx, fake)
    final, answer_mid = await _answer(rig)
    panel, panel_mid = await _panel(rig, final, answer_mid)
    text = panel["text"]
    assert "🧪 DRY RUN (n/a: Telegraph is not GitHub)" in text
    assert "This publishes now, even in dry-run mode." in text
    assert "Only secrets, email, phone and name are checked — read the answer for other personal details first." \
        in text  # fmt: skip
    assert fake.calls == []  # the first tap only opens the panel
    await rig.tap(rig.token_of(panel, "📖 Publish"), message=panel_mid)
    assert [c[0] for c in fake.calls] == ["createPage"]
    form = fake.calls[0][1]
    assert form["access_token"]
    assert form["title"].startswith("Terraform state · ") and len(form["title"].rsplit(" · ", 1)[1]) == 8
    assert tgph.tags_of(fake.nodes()) <= tgph.ALLOWED_TAGS
    assert "explain terraform state" not in form["content"]  # the question is never published
    row = db.one(rig.ctx.conn, "SELECT * FROM telegraph_pages")
    assert row is not None and row["url"].startswith("https://telegra.ph/")
    published = next(p for p in rig.sent("sendMessage") if "Published" in p.get("text", ""))
    assert "public link, not deletable (only blanked)" in published["text"]
    assert {"📖 Open ↗", "📋 Copy link", "🗑 Unpublish"} <= set(_labels(published, rig))


async def test_unpublish_calls_edit_page_with_the_removal_text(rig: TgRig) -> None:
    fake = FakeTelegraph()
    enable(rig.ctx, fake)
    final, answer_mid = await _answer(rig)
    panel, panel_mid = await _panel(rig, final, answer_mid)
    await rig.tap(rig.token_of(panel, "📖 Publish"), message=panel_mid)
    published = next(p for p in rig.sent("sendMessage") if "Published" in p.get("text", ""))
    pub_mid = next(int(r["result_message_id"]) for r in rig.ctx.conn.execute(
        "SELECT result_message_id, payload_json FROM tg_outbox WHERE status = 'sent'")
        if "public link, not deletable" in r["payload_json"])  # fmt: skip
    await rig.tap(rig.token_of(published, "🗑 Unpublish"), message=pub_mid)
    confirm = rig.sent("sendMessage")[-1]
    await rig.tap(rig.token_of(confirm, "🗑 Unpublish"), message=rig.message_id_of(-1))
    method, form = fake.calls[-1]
    assert method == "editPage" and form["title"] == "Removed"
    assert json.loads(form["content"]) == [{"tag": "p", "children": ["This page was removed by its author."]}]
    assert db.one(rig.ctx.conn, "SELECT unpublished_at FROM telegraph_pages")["unpublished_at"] is not None


@pytest.mark.parametrize("tools", [("memory",), ("session_search",), ("ossbot_notify",)])
async def test_no_button_on_turns_that_used_private_tools(rig: TgRig, tools: tuple[str, ...]) -> None:
    fake = FakeTelegraph()
    enable(rig.ctx, fake)
    final, _ = await _answer(rig, tools=tools)
    assert ARTICLE not in _labels(final, rig)


async def test_paused_refuses_every_step_and_calls_nothing(rig: TgRig) -> None:
    fake = FakeTelegraph()
    enable(rig.ctx, fake)
    final, answer_mid = await _answer(rig)
    panel, panel_mid = await _panel(rig, final, answer_mid)
    rig.ctx.control.pause("test", "uid:1000/operator")
    await rig.tap(rig.token_of(final, ARTICLE), message=answer_mid)
    assert "Paused or killed: nothing was published." in json.dumps(rig.req.last("answerCallbackQuery"),
                                                                    ensure_ascii=False)  # fmt: skip
    await rig.tap(rig.token_of(panel, "📖 Publish"), message=panel_mid)
    assert "Not published (paused or killed)." in rig.last_text()
    assert fake.calls == []


async def test_a_pause_between_confirm_and_create_page_sends_nothing(
    rig: TgRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTelegraph()
    enable(rig.ctx, fake)
    final, answer_mid = await _answer(rig)
    panel, panel_mid = await _panel(rig, final, answer_mid)
    real = tgph.scan

    async def pausing_scan(ctx: AppContext, text: str) -> str | None:
        ctx.control.pause("raced", "uid:1000/operator")
        return await real(ctx, text)

    monkeypatch.setattr(tgph, "scan", pausing_scan)
    await rig.tap(rig.token_of(panel, "📖 Publish"), message=panel_mid)
    assert fake.calls == []
    assert "Not published (paused or killed)." in rig.last_text()


async def test_a_profile_email_in_the_answer_is_refused(rig: TgRig) -> None:
    fake = FakeTelegraph()
    enable(rig.ctx, fake)
    final, answer_mid = await _answer(rig, LONG + "\n\nMail test.person@example.org for details.")
    panel, panel_mid = await _panel(rig, final, answer_mid)
    await rig.tap(rig.token_of(panel, "📖 Publish"), message=panel_mid)
    assert fake.calls == []
    assert "Not published: the answer contains personal details" in rig.last_text()


async def test_a_feature_card_after_the_answer_refuses_at_tap_confirm_and_before_create(
        rig: TgRig, monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    """AC 65: tap, Confirm and the check right before createPage all re-read chat_turns.feature_cards."""
    fake = FakeTelegraph()
    enable(rig.ctx, fake)
    final, answer_mid = await _answer(rig)
    panel, panel_mid = await _panel(rig, final, answer_mid)
    real = tgph.scan

    async def card_arrives(ctx: AppContext, text: str) -> str | None:  # accepted between Confirm and createPage
        ctx.conn.execute("UPDATE chat_turns SET feature_cards = 1")
        return await real(ctx, text)

    monkeypatch.setattr(tgph, "scan", card_arrives)
    await rig.tap(rig.token_of(panel, "📖 Publish"), message=panel_mid)
    assert "Not published: this answer is no longer eligible" in rig.last_text()
    await rig.tap(rig.token_of(final, ARTICLE), message=answer_mid)  # the tap now refuses as a toast
    assert "Not eligible" in json.dumps(rig.req.last("answerCallbackQuery"), ensure_ascii=False)
    assert fake.calls == []


async def _confirm_without_settling(rig: TgRig, panel: dict[str, Any], panel_mid: int) -> None:
    data = callback_update(OWNER, OWNER, panel_mid, f"t:{rig.token_of(panel, '📖 Publish')}", "private")
    await rig.app.process_update(Update.de_json(data, rig.app.bot))


async def test_shutdown_awaits_the_shielded_request_so_the_page_is_recorded(rig: TgRig) -> None:
    """F6-4: Handlers.drain (graceful shutdown) waits for a createPage in flight."""
    fake = FakeTelegraph()
    enable(rig.ctx, fake)
    final, answer_mid = await _answer(rig)
    panel, panel_mid = await _panel(rig, final, answer_mid)
    fake.gate = asyncio.Event()
    await _confirm_without_settling(rig, panel, panel_mid)
    for _ in range(50):
        if fake.calls:
            break
        await asyncio.sleep(0.01)
    drain = asyncio.ensure_future(rig.handlers.drain())
    await asyncio.sleep(0.05)
    assert not drain.done()  # still waiting for the shielded request
    fake.gate.set()
    await drain
    assert db.one(rig.ctx.conn, "SELECT COUNT(*) AS n FROM telegraph_pages")["n"] == 1


async def test_kill_before_the_request_leaves_not_published_stopped(rig: TgRig) -> None:
    """AC 68: cancelled before createPage started: nothing is sent to Telegraph."""
    fake = FakeTelegraph()
    enable(rig.ctx, fake)
    rig.ctx.gitleaks_scanner = lambda data: time.sleep(0.3) or []  # the scans are still running
    final, answer_mid = await _answer(rig)
    panel, panel_mid = await _panel(rig, final, answer_mid)
    await _confirm_without_settling(rig, panel, panel_mid)
    await asyncio.sleep(0.05)
    rig.handlers.cancel(KILL_CANCELS)
    await rig.settle()
    await asyncio.sleep(0.4)
    await rig.flush()
    assert fake.calls == []
    assert "Not published (stopped)." in rig.last_text()
    assert db.one(rig.ctx.conn, "SELECT COUNT(*) AS n FROM telegraph_pages")["n"] == 0


async def test_kill_during_create_page_records_and_announces_once(rig: TgRig) -> None:
    """AC 68: the shielded createPage finishes, the page is recorded and /articles lists it."""
    fake = FakeTelegraph()
    enable(rig.ctx, fake)
    final, answer_mid = await _answer(rig)
    panel, panel_mid = await _panel(rig, final, answer_mid)
    fake.gate = asyncio.Event()
    await _confirm_without_settling(rig, panel, panel_mid)
    for _ in range(50):
        if fake.calls:
            break
        await asyncio.sleep(0.01)
    assert [c[0] for c in fake.calls] == ["createPage"]
    rig.handlers.cancel(KILL_CANCELS)
    await asyncio.sleep(0)
    fake.gate.set()
    for _ in range(50):
        if db.one(rig.ctx.conn, "SELECT 1 FROM telegraph_pages") is not None:
            break
        await asyncio.sleep(0.01)
    await rig.settle()
    await rig.flush()
    assert db.one(rig.ctx.conn, "SELECT COUNT(*) AS n FROM telegraph_pages")["n"] == 1
    texts = [p.get("text", "") for m, p in rig.req.calls if m in {"sendMessage", "editMessageText"}]
    assert sum("Published (after stop) · public link · see /articles" in t for t in texts) == 1
    assert not any("public link, not deletable" in t for t in texts)  # exactly one announcement
    await rig.say("/articles")
    assert "Articles" in rig.last_text() and "Terraform state" in rig.last_text()
