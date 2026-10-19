"""The pinned dashboard (DESIGN §14.10.3, D3, D14, D15, AC 14, AC 15; mockup 06)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

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
        assert labels == [["🔄 Refresh", "📥 Approvals (0)"], ["⏸ Pause", "🌙 Quiet 1h"], ["⬇️ Down…", "🛑 Kill"]]
        kill = rig.buttons(payload)[2][1]
        assert kill["style"] == "danger"
        pins = [p for m, p in rig.req.calls if m == "pinChatMessage"]
        assert pins and pins[-1]["message_id"] == _dashboard_id(ctx) and pins[-1]["disable_notification"] is True
        body = payload["text"].split("<i>Updated")[0]
        # the model line follows config/models.yaml (switched live and committed): not part of the golden
        golden.check("dashboard.html", "\n".join(ln for ln in body.splitlines() if not ln.startswith("Models: ")))


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
