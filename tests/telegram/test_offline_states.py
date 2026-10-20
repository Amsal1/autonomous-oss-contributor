"""Error and offline states (DESIGN §14.16, mockup 09; CR3-D15): the killed reply with its reason and
buttons, "⏰ Back online" after a gap, and the short description that tells a full down from a sleep."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest

from ossbot.common.clock import to_iso
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.bot import back_online_text
from ossbot.gatekeeper.telegram.runtime import ui_of
from ossbot.gatekeeper.telegram.ui import classic
from ossbot.gatekeeper.telegram.ui import kit as k
from tests.telegram.reviewkit import labels, sent_with, text
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def _back_online(ctx: AppContext) -> list[tuple[Any, dict[str, Any]]]:
    rows = ctx.conn.execute("SELECT * FROM tg_outbox WHERE payload_json LIKE '%Back online%' ORDER BY id").fetchall()
    return [(r, json.loads(r["payload_json"])) for r in rows]


async def test_killed_reply_names_the_reason_and_offers_resume_and_logs(rig: TgRig) -> None:
    rig.ctx.control.kill("hermes_tool_surface", "tg:4242")
    await rig.say("/queue")
    mid = sent_with(rig, "Killed (soft)")
    body = text(rig, mid)
    assert "<b>Killed (soft)</b> since <tg-time" in body and "reason <code>hermes_tool_surface</code>" in body
    assert "Only /status, /resume, /logs, /help and /start work." in body
    assert labels(rig, mid) == ["▶️ Resume…", "📜 Logs"]
    await rig.say("/status")  # an allowed command still answers
    assert "KILLED" in rig.last_text()


def test_back_online_text() -> None:
    lines = back_online_text(8 * 3600 + 52 * 60, True, 2)
    out = classic.render(k.View(kind="fyi", blocks=[k.Block((k.Lines(lines),))]), surface="test").text
    assert out == "⏰ <b>Back online</b> · offline 8 h 52 m (graceful down) · 2 messages waiting"
    one = classic.render(k.View(kind="fyi", blocks=[k.Block((k.Lines(back_online_text(600, False, 0)),))]),
                         surface="test").text  # fmt: skip
    assert one == "⏰ <b>Back online</b> · offline 10 m"


async def test_back_online_after_a_graceful_down(rig: TgRig) -> None:
    ctx = rig.ctx
    prefs = ui_of(ctx).prefs
    prefs.set("last_seen", to_iso(ctx.clock.now() - timedelta(hours=8, minutes=52)))
    prefs.set("short_description", "💤 Offline since 22:14 — wake the laptop to reconnect")
    ctx.poll_stats.first_batch = None
    await rig.frontend.workspace()  # the start of the gatekeeper
    await rig.frontend.on_tick()
    assert _back_online(ctx) == []  # waits for the first successful poll
    ctx.poll_stats.ok(2)
    await rig.frontend.on_tick()
    await rig.flush()
    [(row, payload)] = _back_online(ctx)
    assert "offline 8 h 52 m (graceful down) · 2 messages waiting" in json.dumps(payload, ensure_ascii=False)
    assert row["priority"] == 1 and row["thread_key"] == "control"
    assert payload.get("disable_notification") is not True  # P1: one loud Control message
    await rig.frontend.on_tick()
    assert len(_back_online(ctx)) == 1  # once


async def test_a_short_restart_gets_no_back_online(rig: TgRig) -> None:
    ctx = rig.ctx
    ui_of(ctx).prefs.set("last_seen", to_iso(ctx.clock.now() - timedelta(minutes=1)))
    ctx.poll_stats.ok(0)
    await rig.frontend.workspace()
    await rig.frontend.on_tick()
    assert _back_online(ctx) == []


async def test_the_description_is_persisted_for_the_next_start(rig: TgRig) -> None:
    rig.ctx.control.down("tg:4242", 0, 0, "distro")
    await rig.frontend.on_tick()
    assert str(ui_of(rig.ctx).prefs.get("short_description")).startswith("💤 Offline since")
