"""``/status`` sends the dashboard card (post-v1 item 3; DESIGN §14.14.1): the same builder as the
pinned dashboard, sent into the topic it was asked from, with its own tokens so taps act on the copy."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.runtime import ui_of
from ossbot.gatekeeper.telegram.ui import classic, rich
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx, topics=True):
        yield r


def _topics(ctx: AppContext) -> dict[str, int]:
    return {r["name"]: int(r["thread_id"]) for r in ctx.conn.execute("SELECT * FROM tg_topics")}


def _last_dashboard(rig: TgRig) -> dict[str, Any]:
    return [p for m, p in rig.req.calls if m == "sendMessage" and "Updated" in str(p.get("text"))][-1]


def _labels(rig: TgRig, payload: dict[str, Any]) -> list[list[str]]:
    return [[b["text"] for b in row] for row in rig.buttons(payload)]


def _purposes(ctx: AppContext, rig: TgRig, payload: dict[str, Any]) -> list[list[str]]:
    out = []
    for row in rig.buttons(payload):
        out.append([str(db.one(ctx.conn, "SELECT purpose FROM ui_tokens WHERE token = ?",
                               (b["callback_data"][2:],))["purpose"]) for b in row])  # fmt: skip
    return out


def test_one_builder_classic_and_rich_renders_are_identical(ctx: AppContext) -> None:
    dash = ui_of(ctx).dashboard
    pinned = dash.render({p: f"tok-{p}" for p in ("dash_refresh", "dash_queue", "dash_pause", "dash_resume",
                                                   "quiet_set", "dash_down", "dash_kill")})  # fmt: skip
    copy = dash.view(None, kind="status")
    assert copy.kind == "status" and pinned.kind == "dashboard"
    assert [b.name for b in copy.blocks] == [b.name for b in pinned.blocks] == ["header", "facts", "footer"]
    assert copy.blocks == pinned.blocks  # block for block
    assert classic.render(copy).text == classic.render(pinned).text
    assert rich.render(copy) == rich.render(pinned)
    assert copy.keyboard_labels() == pinned.keyboard_labels()
    assert [[b.purpose for b in row] for row in copy.rows] == [
        ["dash_refresh", "dash_queue"], ["dash_pause", "quiet_set"], ["dash_down", "dash_kill"]]  # fmt: skip
    assert all(b.token is None for row in copy.rows for b in row)  # fresh tokens, minted at send
    assert len(classic.render(copy).text) <= 1500


@pytest.mark.parametrize("where", ["all", "control", "reviews"])
async def test_status_equals_the_pinned_dashboard_in_the_asking_topic(ctx: AppContext, rig: TgRig, where: str) -> None:
    topics = _topics(ctx)
    thread = {"all": None, "control": topics["control"], "reviews": topics["reviews"]}[where]
    ui_of(ctx).dashboard.send_new()
    await rig.flush()
    dash = _last_dashboard(rig)
    kw: dict[str, Any] = {"thread_id": thread} if thread is not None else {}
    await rig.say("/status", **kw)
    status = rig.sent()[-1]
    assert status["text"] == dash["text"]
    assert _labels(rig, status) == _labels(rig, dash)
    assert _purposes(ctx, rig, status) == _purposes(ctx, rig, dash)
    assert rig.token_of(status, "🔄") != rig.token_of(dash, "🔄")  # its own tokens (I3: bound per message)
    assert status.get("message_thread_id") == thread
    assert dash["message_thread_id"] == topics["control"]
    mid = rig.message_id_of(-1)
    assert db.one(ctx.conn, "SELECT kind FROM tg_views WHERE message_id = ?", (mid,))["kind"] == "status"


async def test_flat_mode_differs_only_by_the_section_icon(ctx: AppContext) -> None:
    async for flat in make_rig(ctx, topics=False):
        ui_of(ctx).dashboard.send_new()
        await flat.flush()
        dash = _last_dashboard(flat)
        await flat.say("/status")
        status = flat.sent()[-1]
        assert dash["text"] == "🚦 " + status["text"]


async def test_refresh_on_a_copy_edits_the_copy(ctx: AppContext, rig: TgRig) -> None:
    await rig.say("/status")
    status = rig.sent()[-1]
    copy_id = rig.message_id_of(-1)
    pinned_id = int(db.one(ctx.conn, "SELECT message_id FROM tg_views WHERE kind = 'dashboard' AND stale = 0")[0])
    rig.req.calls.clear()
    await rig.tap(rig.token_of(status, "🔄"), message=copy_id)
    edits = rig.sent("editMessageText")
    assert copy_id in [e["message_id"] for e in edits]
    copy_edit = [e for e in edits if e["message_id"] == copy_id][-1]
    assert "<b>ossbot</b> · UP · " in copy_edit["text"]
    assert pinned_id != copy_id


async def test_kill_on_a_copy_arms_the_copy(ctx: AppContext, rig: TgRig) -> None:
    await rig.say("/status")
    status = rig.sent()[-1]
    copy_id = rig.message_id_of(-1)
    rig.req.calls.clear()
    await rig.tap(rig.token_of(status, "🛑"), message=copy_id)
    armed = [e for e in rig.sent("editMessageText") if e["message_id"] == copy_id][-1]
    assert _labels(rig, armed)[-1] == ["🛑 Tap again to kill (10 s)"]
    second = rig.token_of(armed, "🛑 Tap again")
    row = db.one(ctx.conn, "SELECT purpose, tg_message_id FROM ui_tokens WHERE token = ?", (second,))
    assert row["purpose"] == "danger_second_tap" and int(row["tg_message_id"]) == copy_id
    await rig.tap(second, message=copy_id)
    assert ctx.control.view().state == "killed"


async def test_status_while_killed_shows_the_killed_header(ctx: AppContext, rig: TgRig) -> None:
    ctx.control.kill("test", "tg:1", "soft")
    await rig.say("/status")
    text = rig.sent()[-1]["text"]
    assert "🛑" in text and "KILLED (soft)" in text
    assert "Reason: test · since " in text
