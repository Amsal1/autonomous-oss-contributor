"""The pinned dashboard (DESIGN §14.10.3, D3, D14, D15, AC 14, AC 15; mockup 06)."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ossbot.common.clock import FakeClock
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.dashboard import DASHBOARD_MAX
from ossbot.gatekeeper.telegram.runtime import ui_of
from tests.telegram import golden
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def _dashboard_id(ctx: AppContext) -> int:
    return int(db.one(ctx.conn, "SELECT message_id FROM tg_views WHERE kind = 'dashboard' AND stale = 0")[0])


async def test_start_sends_one_dashboard_while_the_first_is_queued(ctx: AppContext) -> None:
    """The live start sent and pinned three dashboards (workspace + the first two ticks before delivery)."""
    async for rig in make_rig(ctx):
        ctx.conn.execute("UPDATE tg_views SET stale = 1 WHERE kind = 'dashboard'")  # as on a fresh chat
        rig.frontend._lifecycle_seen = None
        await rig.frontend.workspace()
        await rig.frontend.on_tick()
        await rig.frontend.on_tick()
        queued = ctx.conn.execute("SELECT COUNT(*) FROM tg_outbox WHERE status = 'pending' AND method = 'sendMessage'"
                                  " AND json_extract(view_json, '$.kind') = 'dashboard'").fetchone()[0]  # fmt: skip
        assert queued == 1
        await rig.flush()
        assert len([m for m, _ in rig.req.calls if m == "pinChatMessage"]) == 1


async def test_dashboard_is_sent_pinned_and_golden(ctx: AppContext) -> None:
    async for rig in make_rig(ctx):
        sent = [p for m, p in rig.req.calls if m == "sendMessage"]
        assert sent == []  # the harness clears the workspace calls; re-send to inspect
        ui_of(ctx).dashboard.send_new()
        await rig.flush()
        payload = rig.sent()[-1]
        assert payload["disable_notification"] is True
        assert len(payload["text"]) <= DASHBOARD_MAX
        # flat mode (no topics in this fake): the 🚦 section icon comes first (§14.10.1)
        assert payload["text"].startswith("🚦 🟢 <b>ossbot</b> · UP · <b>🧪 DRY RUN</b>")
        assert '<i>Updated <tg-time unix="' in payload["text"] and 'format="r">' in payload["text"]
        labels = [[b["text"] for b in row] for row in rig.buttons(payload)]
        # telegram.yaml command_shortcuts (default on) adds the two command rows (tests/telegram/test_shortcuts.py)
        assert labels == [["🔄 Refresh", "📥 Approvals (0)"], ["⏸ Pause", "🌙 Quiet 1h"],
                          ["🛠 Work", "📏 Size", "🔎 Filters"], ["🤖 Model", "❓ Help"],
                          ["⬇️ Down…", "🛑 Kill"]]  # fmt: skip
        kill = rig.buttons(payload)[-1][1]
        assert kill["style"] == "danger"
        pins = [p for m, p in rig.req.calls if m == "pinChatMessage"]
        assert pins and pins[-1]["message_id"] == _dashboard_id(ctx) and pins[-1]["disable_notification"] is True
        body = payload["text"].split("<i>Updated")[0]
        # the model line follows config/models.yaml (switched live and committed) and the config/audit heads
        # follow the fixture config and audit rows: not part of the golden, their shape is checked instead
        heads = [ln for ln in body.splitlines() if ln.startswith("Config ")]
        assert len(heads) == 1 and re.fullmatch(
            r"Config <code>[0-9a-f]{12}</code> · audit <code>[0-9a-f]{12}</code> · anchor none yet", heads[0]
        ), heads
        assert "Today: PRs 0/" in body and " · comments 0/" in body and " · claims 0/" in body
        kept = [ln for ln in body.splitlines() if not ln.startswith(("Models: ", "Config "))]
        golden.check("dashboard.html", "\n".join(kept))


async def test_edits_are_throttled_and_skipped_when_unchanged(ctx: AppContext, rig: TgRig, clock: FakeClock) -> None:
    dash = ui_of(ctx).dashboard
    clock.advance(timedelta(seconds=31))
    assert dash.tick() is False  # nothing changed but the "Updated" time: no edit
    ctx.control.pause("test", "tg:4242")
    assert dash.tick() is True
    await rig.flush()
    edit = rig.req.last("editMessageText")
    assert edit["message_id"] == _dashboard_id(ctx) and "⏸ <b>ossbot</b> · PAUSED ·" in edit["text"]
    ctx.control.kill("test", "tg:4242")
    assert dash.tick() is False  # < 30 s since the last edit
    await rig.flush()  # the kill FYI is a due P0 alert, which the dashboard edit waits behind (§14.10.3)
    clock.advance(timedelta(seconds=30))
    assert dash.tick() is True


async def test_deleted_dashboard_is_resent_and_pinned(ctx: AppContext, rig: TgRig, clock: FakeClock) -> None:
    old = _dashboard_id(ctx)
    ctx.control.pause("test", "tg:4242")
    rig.req.fail("editMessageText", "Bad Request: message to edit not found")
    clock.advance(timedelta(seconds=31))
    ui_of(ctx).dashboard.tick()
    await rig.flush()
    new = _dashboard_id(ctx)
    assert new != old
    assert rig.req.last("pinChatMessage")["message_id"] == new


async def test_kill_needs_a_second_tap_within_10_seconds(ctx: AppContext, rig: TgRig, clock: FakeClock) -> None:
    mid = _dashboard_id(ctx)
    tokens = db.one(ctx.conn, "SELECT state_json FROM tg_views WHERE message_id = ?", (mid,))[0]
    import json

    kill = json.loads(tokens)["tokens"]["dash_kill"]
    await rig.tap(kill, message=mid)
    assert ctx.control.view().state == "running"
    second = rig.token_of(rig.req.last("editMessageText"), "🛑 Tap again")
    clock.advance(timedelta(seconds=11))
    await rig.tap(second, message=mid)
    assert ctx.control.view().state == "running"  # too late (AC 15)
    assert "expired" in rig.req.last("answerCallbackQuery")["text"]
    await rig.tap(kill, message=mid)
    second = rig.token_of(rig.req.last("editMessageText"), "🛑 Tap again")
    clock.advance(timedelta(seconds=5))
    await rig.tap(second, message=mid)
    assert ctx.control.view().state == "killed"


def _pins(rig: TgRig) -> list[int]:
    return [int(p["message_id"]) for m, p in rig.req.calls if m == "pinChatMessage"]


async def test_threaded_mode_pins_only_a_dashboard_in_control(ctx: AppContext, caplog: pytest.LogCaptureFixture
                                                              ) -> None:  # fmt: skip
    """pinChatMessage names no thread (Bot API 10.3): the pin follows the message's topic (item 4)."""
    caplog.set_level(logging.INFO)
    async for rig in make_rig(ctx, topics=True):
        control = int(db.one(ctx.conn, "SELECT thread_id FROM tg_topics WHERE name = 'control'")[0])
        dash = ui_of(ctx).dashboard
        dash.send_new()
        await rig.flush()
        sent = [p for m, p in rig.req.calls if m == "sendMessage" and "Updated" in str(p.get("text"))][-1]
        assert sent["message_thread_id"] == control
        mid = _dashboard_id(ctx)
        assert _pins(rig)[-1] == mid
        state = json.loads(db.one(ctx.conn, "SELECT state_json FROM tg_views WHERE message_id = ?", (mid,))[0])
        assert state["sent_thread"] == control
        assert any(getattr(r, "event", None) == "dashboard_pinned" and f"thread={control}" in r.getMessage()
                   for r in caplog.records)  # fmt: skip
        # a dashboard whose send result is outside Control: not pinned, retired, re-sent once into Control
        rig.req.calls.clear()
        dash.on_sent(cast(Any, SimpleNamespace(method="sendMessage")), {"message_id": mid})
        stale = db.one(ctx.conn, "SELECT stale FROM tg_views WHERE message_id = ?", (mid,))[0]
        assert stale == 1 and _pins(rig) == []
        await rig.flush()
        resent = [p for m, p in rig.req.calls if m == "sendMessage" and "Updated" in str(p.get("text"))]
        assert len(resent) == 1 and resent[0]["message_thread_id"] == control
        new = _dashboard_id(ctx)
        assert new != mid and _pins(rig) == [new]
        assert any(getattr(r, "event", None) == "dashboard_resent_to_control" for r in caplog.records)
        # only once per start: a second miss is pinned where it is (documented fallback)
        rig.req.calls.clear()
        dash.on_sent(cast(Any, SimpleNamespace(method="sendMessage")), {"message_id": new})
        await rig.flush()
        assert _pins(rig) == [new]


async def test_pin_failure_is_logged(ctx: AppContext, rig: TgRig, caplog: pytest.LogCaptureFixture) -> None:
    rig.req.fail("pinChatMessage", "Bad Request: not enough rights to manage pinned messages in the chat")
    ui_of(ctx).dashboard.send_new()
    await rig.flush()
    assert any(getattr(r, "event", None) == "dashboard_pin_failed" for r in caplog.records)


async def test_pause_and_quiet_buttons(ctx: AppContext, rig: TgRig) -> None:
    import json

    mid = _dashboard_id(ctx)
    tokens = json.loads(db.one(ctx.conn, "SELECT state_json FROM tg_views WHERE message_id = ?", (mid,))[0])["tokens"]
    await rig.tap(tokens["quiet_set"], message=mid)
    assert ui_of(ctx).prefs.until("quiet_until") is not None
    await rig.tap(tokens["dash_pause"], message=mid)
    assert ctx.control.view().state == "paused"
    await rig.tap(tokens["dash_pause"], message=mid + 1)  # bound to the dashboard message only
    assert rig.req.last("answerCallbackQuery").get("text") == "This button is no longer valid."
