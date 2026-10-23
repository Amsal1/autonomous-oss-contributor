"""``/status`` sends the dashboard card (post-v1 item 3; DESIGN §14.14.1): the same builder as the
pinned dashboard, sent into the topic it was asked from, with its own tokens so taps act on the copy."""

from __future__ import annotations

import logging
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
    pinned = dash.render({p: f"tok-{p}" for p in dash.purposes()})
    copy = dash.view(None, kind="status")
    assert copy.kind == "status" and pinned.kind == "dashboard"
    assert [b.name for b in copy.blocks] == [b.name for b in pinned.blocks] == ["header", "facts", "footer"]
    assert copy.blocks == pinned.blocks  # block for block
    assert classic.render(copy).text == classic.render(pinned).text
    assert rich.render(copy) == rich.render(pinned)
    assert copy.keyboard_labels() == pinned.keyboard_labels()
    # command_shortcuts is on by default: its two rows sit between Pause/Quiet and Down/Kill
    assert [[b.purpose for b in row] for row in copy.rows] == [
        ["dash_refresh", "dash_queue"], ["dash_pause", "quiet_set"], ["sc_work", "sc_size", "sc_filters"],
        ["sc_model", "sc_help"], ["dash_down", "dash_kill"]]  # fmt: skip
    assert all(b.token is None for row in copy.rows for b in row)  # fresh tokens, minted at send
    assert len(classic.render(copy).text) <= 1500


@pytest.mark.parametrize(("mode", "value", "shown"), [
    ("open", "off", "Impl. network: off ·"), ("open", "auto", "Impl. network: auto (inactive) ·"),
    ("sandbox_open", "on_request", "Impl. network: on_request ·"),
])  # fmt: skip
def test_status_shows_the_implement_network(ctx: AppContext, mode: str, value: str, shown: str) -> None:
    """ADR-0024 / R2.12: the setting and whether it is active (inactive outside sandbox_open)."""
    default = ctx.config.egress.mode_default
    if mode != default:
        ctx.control.set_egress_mode(mode, "tg:4242", default, egress=ctx.config.egress)  # type: ignore[arg-type]
    ctx.control.set_implement_network(value, "tg:4242", default)  # type: ignore[arg-type]
    body = classic.render(ui_of(ctx).dashboard.view(None, kind="status")).text
    assert shown in body and (("(inactive)" in body) is (value != "off" and mode != "sandbox_open"))


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
    assert "deleteForumTopic" not in rig.req.methods()
    assert dash["message_thread_id"] == topics["control"]
    mid = rig.message_id_of(-1)
    assert db.one(ctx.conn, "SELECT kind FROM tg_views WHERE message_id = ?", (mid,))["kind"] == "status"


STRAY = 5150  # the topic Telegram creates for a message typed in "All" (an id the bot never made)
IMPLICIT = {"name": "/status", "icon_color": 7322096, "is_name_implicit": True}
EXPLICIT = {"name": "My notes", "icon_color": 7322096}


def _deletes(rig: TgRig) -> list[int]:
    return [int(p["message_thread_id"]) for m, p in rig.req.calls if m == "deleteForumTopic"]


def _status_after_dashboard(ctx: AppContext, rig: TgRig, dash: dict[str, Any]) -> dict[str, Any]:
    status = rig.sent()[-1]
    assert status["text"] == dash["text"]
    mid = rig.message_id_of(-1)
    assert db.one(ctx.conn, "SELECT kind FROM tg_views WHERE message_id = ?", (mid,))["kind"] == "status"
    return status


@pytest.mark.parametrize("is_topic", [True, False])
async def test_status_from_all_with_an_implicit_topic_answers_in_all_and_deletes_it(
    ctx: AppContext, rig: TgRig, is_topic: bool
) -> None:
    ui_of(ctx).dashboard.send_new()
    await rig.flush()
    dash = _last_dashboard(rig)
    rig.req.calls.clear()
    await rig.say("/status", thread_id=STRAY, topic_created=IMPLICIT, is_topic=is_topic)
    status = _status_after_dashboard(ctx, rig, dash)
    assert "message_thread_id" not in status  # All
    assert _deletes(rig) == [STRAY]
    methods = rig.req.methods()
    assert methods.index("sendMessage") < methods.index("deleteForumTopic")  # the reply first


async def test_status_from_all_without_a_thread_id_deletes_nothing(ctx: AppContext, rig: TgRig) -> None:
    await rig.say("/status")  # true All, or a client that omits the new topic's id (the documented limit)
    assert "message_thread_id" not in rig.sent()[-1]
    assert _deletes(rig) == []


async def test_thread_without_is_topic_message_is_answered_in_that_thread(ctx: AppContext, rig: TgRig) -> None:
    await rig.say("/status", thread_id=STRAY, is_topic=False)  # S2: no implicit flag
    assert rig.sent()[-1]["message_thread_id"] == STRAY
    assert _deletes(rig) == []


@pytest.mark.parametrize("where", ["control", "reviews"])
async def test_system_topics_answer_in_place_and_are_never_deleted(ctx: AppContext, rig: TgRig, where: str) -> None:
    thread = _topics(ctx)[where]
    await rig.say("/status", thread_id=thread, topic_created=IMPLICIT)
    assert rig.sent()[-1]["message_thread_id"] == thread
    assert _deletes(rig) == []


async def test_user_created_topic_answers_in_place(ctx: AppContext, rig: TgRig) -> None:
    await rig.say("/status", thread_id=4711, topic_created=EXPLICIT)
    assert rig.sent()[-1]["message_thread_id"] == 4711
    assert _deletes(rig) == []


async def test_second_message_in_an_implicit_topic_answers_there(ctx: AppContext, rig: TgRig) -> None:
    await rig.say("hi", thread_id=STRAY, topic_created={**IMPLICIT, "name": "hi"})
    rig.req.calls.clear()
    await rig.say("/status", thread_id=STRAY, topic_created={**IMPLICIT, "name": "hi"})
    assert rig.sent()[-1]["message_thread_id"] == STRAY
    assert _deletes(rig) == []


@pytest.mark.parametrize(("command", "expect"), [("/help", "/status"), ("/title x", "Only a chat thread")])
async def test_other_commands_from_all_answer_in_all_and_delete_their_topic(
    ctx: AppContext, rig: TgRig, command: str, expect: str
) -> None:
    await rig.say(command, thread_id=STRAY, topic_created={**IMPLICIT, "name": command})
    reply = rig.sent()[-1]
    assert "message_thread_id" not in reply and expect in reply["text"]
    assert _deletes(rig) == [STRAY]


async def test_flat_mode_never_deletes_a_topic(ctx: AppContext) -> None:
    async for flat in make_rig(ctx, topics=False):
        ui_of(ctx).dashboard.send_new()
        await flat.flush()
        dash = _last_dashboard(flat)
        await flat.say("/status", thread_id=STRAY, topic_created=IMPLICIT)
        assert _deletes(flat) == []
        assert dash["text"] == "🚦 " + flat.sent()[-1]["text"]


async def test_delete_failure_is_logged_and_not_retried(
    ctx: AppContext, rig: TgRig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.req.fail("deleteForumTopic", "Bad Request: TOPIC_ID_INVALID")
    with caplog.at_level(logging.INFO):
        await rig.say("/status", thread_id=STRAY, topic_created=IMPLICIT)
        await rig.flush()
    assert "Updated" in rig.sent()[-1]["text"]  # the card is still sent
    assert _deletes(rig) == [STRAY]  # one attempt only
    assert [r for r in caplog.records if getattr(r, "event", None) == "stray_topic_delete_failed"]


async def test_thread_fields_are_logged_without_text(
    ctx: AppContext, rig: TgRig, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "/status zebra-quokka-7731"
    with caplog.at_level(logging.INFO):
        await rig.say(secret, thread_id=STRAY, topic_created=IMPLICIT)
    fields = [r for r in caplog.records if getattr(r, "event", None) == "tg_thread_fields"]
    assert len(fields) == 1
    rec = fields[0]
    assert rec.message_thread_id == STRAY and rec.is_name_implicit is True and rec.is_command is True
    assert rec.reply_topic_created is True and rec.has_direct_messages_topic is False
    target = [r for r in caplog.records if getattr(r, "event", None) == "command_reply_target"]
    assert [(r.where, r.reply_thread, r.delete_thread) for r in target] == [("stray", None, STRAY)]
    for r in caplog.records:  # neither the message nor any extra carries the text
        assert "zebra-quokka" not in r.getMessage()
        assert "zebra-quokka" not in str(vars(r))


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
