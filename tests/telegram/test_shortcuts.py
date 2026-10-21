"""Tappable command shortcuts (post-v1 plan D9, ``telegram/shortcuts.py``, ``telegram.yaml command_shortcuts``).

Inline buttons send no message, so they never make Telegram open an implicit topic in "All". With the
flag on (the default) the dashboard and its /status copies, the /help menu and button-less command
replies in All carry them; with it off every keyboard is exactly the pre-shortcut one."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.dashboard import PURPOSES, SHORTCUT_PURPOSES
from ossbot.gatekeeper.telegram.runtime import ui_of
from ossbot.gatekeeper.telegram.shortcuts import shortcut_rows
from ossbot.gatekeeper.telegram.ui import classic, rich
from ossbot.gatekeeper.telegram.ui.limits import CALLBACK_DATA_BYTES, check_keyboard
from tests.fakes.fake_telegram import callback_update
from tests.telegram import golden
from tests.telegram.tgharness import OWNER, TgRig, make_rig

TODAY = [["🔄 Refresh", "📥 Approvals (0)"], ["⏸ Pause", "🌙 Quiet 1h"], ["⬇️ Down…", "🛑 Kill"]]
WITH_SHORTCUTS = [["🔄 Refresh", "📥 Approvals (0)"], ["⏸ Pause", "🌙 Quiet 1h"],
                  ["🛠 Work", "📏 Size", "🔎 Filters"], ["🤖 Model", "❓ Help"], ["⬇️ Down…", "🛑 Kill"]]  # fmt: skip
HELP_MENU = [["🚦 Status", "📥 Queue", "🛠 Work"], ["📏 Size", "🔎 Filters", "🤖 Model"], ["⏸ Pause", "▶️ Resume…"]]
COMPACT = [["🚦 Status", "📥 Queue", "❓ Help"]]
STRAY = 5150
IMPLICIT = {"name": "/dashboard", "icon_color": 7322096, "is_name_implicit": True}


def _off(ctx: AppContext) -> None:
    ctx.config.telegram.__dict__["command_shortcuts"] = False  # as if telegram.yaml said false (frozen model)


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx, topics=True):
        yield r


def _labels(rig: TgRig, payload: dict[str, Any]) -> list[list[str]]:
    return [[b["text"] for b in row] for row in rig.buttons(payload)]


def _purposes(ctx: AppContext, rig: TgRig, payload: dict[str, Any]) -> list[list[str]]:
    return [[str(db.one(ctx.conn, "SELECT purpose FROM ui_tokens WHERE token = ?", (b["callback_data"][2:],))[0])
             for b in row] for row in rig.buttons(payload)]  # fmt: skip


def _control(ctx: AppContext) -> int:
    return int(db.one(ctx.conn, "SELECT thread_id FROM tg_topics WHERE name = 'control'")[0])


def _pinned(ctx: AppContext) -> tuple[int, dict[str, str]]:
    row = db.one(ctx.conn, "SELECT message_id, state_json FROM tg_views WHERE kind = 'dashboard' AND stale = 0")
    return int(row["message_id"]), json.loads(row["state_json"])["tokens"]


def _keyboards_golden(rig: TgRig, payloads: dict[str, dict[str, Any]]) -> str:
    return "\n".join(f"{name}: {json.dumps(_labels(rig, p), ensure_ascii=False)}" for name, p in payloads.items())


async def _tap_in(rig: TgRig, token: str, message: int, thread_id: int | None) -> None:
    data = callback_update(OWNER, OWNER, message, f"t:{token}")
    if thread_id is not None:
        data["callback_query"]["message"].update({"message_thread_id": thread_id, "is_topic_message": True})
    await rig.send(data)


# ------------------------------------------------------------------ dashboard
@pytest.mark.parametrize("on", [True, False])
async def test_dashboard_and_status_copy_rows(ctx: AppContext, on: bool) -> None:
    if not on:
        _off(ctx)
    async for rig in make_rig(ctx, topics=True):
        dash = ui_of(ctx).dashboard
        dash.send_new()
        await rig.flush()
        pinned = [p for m, p in rig.req.calls if m == "sendMessage" and "Updated" in str(p.get("text"))][-1]
        await rig.say("/status")
        copy = rig.sent()[-1]
        expected = WITH_SHORTCUTS if on else TODAY
        assert _labels(rig, pinned) == _labels(rig, copy) == expected
        assert _purposes(ctx, rig, pinned) == _purposes(ctx, rig, copy)
        flat = {p for row in _purposes(ctx, rig, pinned) for p in row}
        assert flat == set(dash.purposes()) - {"dash_resume"}  # Pause shows while running
        assert set(dash.purposes()) == set(PURPOSES) | (set(SHORTCUT_PURPOSES) if on else set())
        assert set(_pinned(ctx)[1]) == set(dash.purposes())
        golden.check(f"shortcuts_dashboard_{'on' if on else 'off'}.txt",
                     _keyboards_golden(rig, {"dashboard": pinned, "status": copy}) + "\n")  # fmt: skip


async def test_shortcut_rows_do_not_change_the_dashboard_text_classic_or_rich(ctx: AppContext) -> None:
    dash = ui_of(ctx).dashboard
    on = dash.view(None, kind="status")
    _off(ctx)
    off = dash.view(None, kind="status")
    assert classic.render(on).text == classic.render(off).text
    assert rich.render(on) == rich.render(off)
    assert len(on.rows) == 5 and len(off.rows) == 3
    on.check()  # ≤ 6 rows, ≤ 3 buttons per row, label lengths (ui/limits.py)


async def test_toggling_the_flag_re_mints_the_pinned_token_set_once(ctx: AppContext) -> None:
    _off(ctx)
    async for rig in make_rig(ctx, topics=True):
        dash = ui_of(ctx).dashboard
        mid, before = _pinned(ctx)
        assert set(before) == set(PURPOSES)
        ctx.config.telegram.__dict__["command_shortcuts"] = True
        assert dash.tick(force=True) is True
        await rig.flush()
        assert _pinned(ctx)[0] == mid
        after = _pinned(ctx)[1]
        assert set(after) == set(PURPOSES) | set(SHORTCUT_PURPOSES)
        edit = rig.req.last("editMessageText")
        assert edit["message_id"] == mid and _labels(rig, edit) == WITH_SHORTCUTS
        dash.tick(force=True)
        assert _pinned(ctx)[1] == after  # reused from then on


async def test_pinned_dashboard_shortcut_replies_land_in_control(ctx: AppContext, rig: TgRig) -> None:
    mid, tokens = _pinned(ctx)
    control = _control(ctx)
    rig.req.calls.clear()
    await _tap_in(rig, tokens["sc_help"], mid, control)
    reply = rig.sent()[-1]
    assert reply["message_thread_id"] == control and "/status — " in reply["text"]
    assert _labels(rig, reply) == HELP_MENU


# ------------------------------------------------------------------ /help
async def test_help_menu_keeps_the_command_lines_and_has_no_kill(ctx: AppContext, rig: TgRig) -> None:
    await rig.say("/help", thread_id=_control(ctx))
    on = rig.sent()[-1]
    assert _labels(rig, on) == HELP_MENU
    purposes = {p for row in _purposes(ctx, rig, on) for p in row}
    assert purposes == {"sc_status", "dash_queue", "sc_work", "sc_size", "sc_filters", "sc_model", "dash_pause",
                        "dash_resume"}  # fmt: skip
    assert not any(label.startswith("🛑") for row in _labels(rig, on) for label in row)
    assert all(len(b["callback_data"]) == CALLBACK_DATA_BYTES for row in rig.buttons(on) for b in row)
    check_keyboard(_labels(rig, on))
    _off(ctx)
    await rig.say("/help", thread_id=_control(ctx))
    off = rig.sent()[-1]
    assert "reply_markup" not in off
    assert on["text"] == off["text"]  # the same /name — help lines, so the command links still work
    golden.check("shortcuts_help.txt", _keyboards_golden(rig, {"help_on": on, "help_off": off}) + "\n")


async def test_help_view_renders_rich_without_buttons(ctx: AppContext) -> None:
    rows = shortcut_rows(ctx, "help")
    assert [[b.label for b in row] for row in rows] == HELP_MENU
    assert [[b.label for b in row] for row in shortcut_rows(ctx, "compact")] == COMPACT
    _off(ctx)
    assert shortcut_rows(ctx, "help") == [] and shortcut_rows(ctx, "compact") == []


@pytest.mark.parametrize("rich_views", ["on", "off"])
async def test_rich_views_pref_does_not_change_the_keyboards(ctx: AppContext, rig: TgRig, rich_views: str) -> None:
    ui_of(ctx).prefs.set("rich_views", rich_views)
    rig.req.calls.clear()
    await rig.say("/help")
    await rig.say("/dashboard")
    methods = [m for m, _ in rig.req.calls if m.startswith("send")]
    assert "sendRichMessage" not in methods
    help_, dashboard = _reply(rig, "/status — "), _reply(rig, "Dashboard re-sent")
    assert _labels(rig, help_) == HELP_MENU and _labels(rig, dashboard) == COMPACT


# ------------------------------------------------------------------ compact row on replies in All
def _reply(rig: TgRig, needle: str) -> dict[str, Any]:
    return [p for p in rig.sent() if needle in str(p.get("text"))][-1]


@pytest.mark.parametrize(("command", "needle"), [("/dashboard", "Dashboard re-sent"), ("/work", "/work"),
                                                 ("/nosuchcommand", "Unknown command")])  # fmt: skip
async def test_buttonless_reply_typed_in_all_gets_the_compact_row(ctx: AppContext, rig: TgRig, command: str,
                                                                  needle: str) -> None:  # fmt: skip
    await rig.say(command)
    reply = _reply(rig, needle)
    assert "message_thread_id" not in reply
    assert _labels(rig, reply) == COMPACT
    assert {p for row in _purposes(ctx, rig, reply) for p in row} == {"sc_status", "dash_queue", "sc_help"}


async def test_compact_row_with_the_implicit_topic_deleted(ctx: AppContext, rig: TgRig) -> None:
    await rig.say("/dashboard", thread_id=STRAY, topic_created=IMPLICIT)
    reply = _reply(rig, "Dashboard re-sent")
    assert "message_thread_id" not in reply and _labels(rig, reply) == COMPACT
    assert [p["message_thread_id"] for m, p in rig.req.calls if m == "deleteForumTopic"] == [STRAY]


@pytest.mark.parametrize("command", ["/queue", "/status", "/help"])
async def test_replies_with_their_own_rows_get_no_compact_row(ctx: AppContext, rig: TgRig, command: str) -> None:
    await rig.say(command)
    labels = _labels(rig, rig.sent()[-1])
    assert labels and COMPACT[0] not in labels


async def test_no_compact_row_in_control_a_user_topic_or_flat_mode(ctx: AppContext, rig: TgRig) -> None:
    await rig.say("/dashboard", thread_id=_control(ctx))
    assert "reply_markup" not in _reply(rig, "Dashboard re-sent")
    await rig.say("/dashboard", thread_id=4711, topic_created={"name": "notes", "icon_color": 7322096})
    assert "reply_markup" not in _reply(rig, "Dashboard re-sent")
    async for flat in make_rig(ctx, topics=False):
        await flat.say("/dashboard")
        assert "reply_markup" not in _reply(flat, "Dashboard re-sent")


async def test_flag_off_means_no_compact_row(ctx: AppContext, rig: TgRig) -> None:
    _off(ctx)
    await rig.say("/dashboard")
    assert "reply_markup" not in _reply(rig, "Dashboard re-sent")
    await rig.say("/work")
    assert "reply_markup" not in _reply(rig, "/work")


# ------------------------------------------------------------------ taps
async def _help_in_all(rig: TgRig) -> tuple[dict[str, Any], int]:
    await rig.say("/help")
    return rig.sent()[-1], rig.message_id_of(-1)


@pytest.mark.parametrize(("label", "expect", "compact"), [
    ("🚦 Status", "Updated", False),
    ("🛠 Work", "/work", True),
    ("📏 Size", "", None),
    ("🔎 Filters", "", None),
    ("🤖 Model", "", None),
])  # fmt: skip
async def test_each_shortcut_runs_its_command_in_the_tapped_thread(
    ctx: AppContext, rig: TgRig, label: str, expect: str, compact: bool | None
) -> None:
    menu, mid = await _help_in_all(rig)
    name = {"🚦 Status": "status", "🛠 Work": "work", "📏 Size": "size", "🔎 Filters": "filters",
            "🤖 Model": "model"}[label]  # fmt: skip
    await rig.say(f"/{name}", thread_id=_control(ctx))
    typed = rig.sent()[-1]
    rig.req.calls.clear()
    await rig.tap(rig.token_of(menu, label), message=mid)
    sends = rig.sent()
    assert len(sends) == 1
    reply = sends[0]
    assert "message_thread_id" not in reply  # the menu is in All, so the reply is too
    assert expect in reply["text"]
    assert reply["text"] == typed["text"]  # the same command function as typing it
    if compact is True:
        assert _labels(rig, reply) == COMPACT
    elif compact is False:
        assert COMPACT[0] not in _labels(rig, reply)
    else:  # pickers keep their own rows, the same as the typed command's
        assert _labels(rig, reply) == _labels(rig, typed)
    assert "deleteForumTopic" not in rig.req.methods()  # a tap sends no owner message: no implicit topic


async def test_help_tap_in_a_topic_replies_there_without_the_compact_row(ctx: AppContext, rig: TgRig) -> None:
    await rig.say("/work")
    compact, mid = rig.sent()[-1], rig.message_id_of(-1)
    assert _labels(rig, compact) == COMPACT
    rig.req.calls.clear()
    await _tap_in(rig, rig.token_of(compact, "❓ Help"), mid, None)
    assert _labels(rig, rig.sent()[-1]) == HELP_MENU and "message_thread_id" not in rig.sent()[-1]
    await rig.say("/help", thread_id=4711, topic_created={"name": "notes", "icon_color": 7322096})
    menu, menu_id = rig.sent()[-1], rig.message_id_of(-1)
    rig.req.calls.clear()
    await _tap_in(rig, rig.token_of(menu, "🛠 Work"), menu_id, 4711)
    reply = rig.sent()[-1]
    assert reply["message_thread_id"] == 4711 and "reply_markup" not in reply


async def test_pause_is_immediate_and_leaves_the_menu_alone(ctx: AppContext, rig: TgRig) -> None:
    menu, mid = await _help_in_all(rig)
    rig.req.calls.clear()
    await rig.tap(rig.token_of(menu, "⏸ Pause"), message=mid)
    assert ctx.control.view().state == "paused"
    assert mid not in [p["message_id"] for p in rig.sent("editMessageText")]  # not turned into a dashboard
    assert rig.req.last("answerCallbackQuery")["text"] == "⏸ Paused."


async def test_resume_opens_its_confirm(ctx: AppContext, rig: TgRig) -> None:
    ctx.control.pause("test", "tg:4242")
    menu, mid = await _help_in_all(rig)
    rig.req.calls.clear()
    await rig.tap(rig.token_of(menu, "▶️ Resume…"), message=mid)
    assert ctx.control.view().state == "paused"
    confirm = rig.sent()[-1]
    assert "resume_confirm" in {p for row in _purposes(ctx, rig, confirm) for p in row}


async def test_model_keeps_its_confirm(ctx: AppContext, rig: TgRig) -> None:
    menu, mid = await _help_in_all(rig)
    rig.req.calls.clear()
    await rig.tap(rig.token_of(menu, "🤖 Model"), message=mid)
    purposes = {p for row in _purposes(ctx, rig, rig.sent()[-1]) for p in row}
    assert purposes and "model_confirm" not in purposes  # the role picker first, the confirm last


async def test_killed_mode_applies_as_for_typed_commands(ctx: AppContext, rig: TgRig) -> None:
    menu, mid = await _help_in_all(rig)
    ctx.control.kill("test", "tg:4242", "soft")
    rig.req.calls.clear()
    await rig.tap(rig.token_of(menu, "🛠 Work"), message=mid)
    assert "Only /status, /resume, /logs, /help and /start work." in rig.sent()[-1]["text"]  # the killed view
    rig.req.calls.clear()
    await rig.tap(rig.token_of(menu, "🚦 Status"), message=mid)
    assert "KILLED (soft)" in rig.sent()[-1]["text"]
