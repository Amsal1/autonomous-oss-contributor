"""A command typed in "All" arrives as two updates (journal ``tg_thread_fields``, 2026-10-20): update A
is the ``forum_topic_created`` service message of a NEW thread (message N), update B the command with no
thread (message N+1). The pair is correlated and only that thread is deleted, after the reply in All
(ADR-0020 addendum; ``telegram/implicit_topics.py``)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.implicit_topics import (
    CommandSeen,
    ImplicitTopics,
    Match,
    TopicSeen,
    command_key,
    name_matches,
)
from ossbot.gatekeeper.telegram.runtime import ui_of
from tests.fakes.fake_telegram import message_update, topic_created_update
from tests.telegram.tgharness import OWNER, TgRig, make_rig

NEW = 125061  # a thread id Telegram assigns (synthetic; the bot never created it)
N = 6000  # message id of update A; the command is N + 1


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx, topics=True):
        yield r


def _deletes(rig: TgRig) -> list[int]:
    return [int(p["message_thread_id"]) for m, p in rig.req.calls if m == "deleteForumTopic"]


def _a(*, thread: int = NEW, mid: int = N, name: str = "/status", implicit: bool | None = None) -> dict[str, Any]:
    return topic_created_update(OWNER, OWNER, thread_id=thread, message_id=mid, name=name, implicit=implicit)


def _b(text: str = "/status", *, mid: int = N + 1) -> dict[str, Any]:
    return message_update(OWNER, OWNER, text, message_id=mid)  # no thread: typed in All


def _assert_reply_in_all_then_delete(rig: TgRig, thread: int = NEW) -> None:
    replies = rig.sent()
    assert replies and "message_thread_id" not in replies[-1]
    assert _deletes(rig) == [thread]
    methods = rig.req.methods()
    assert methods.index("sendMessage") < methods.index("deleteForumTopic")


async def test_topic_then_command_deletes_the_topic_after_the_reply(ctx: AppContext, rig: TgRig) -> None:
    await rig.send(_a())
    assert _deletes(rig) == []  # nothing until the command arrives
    ctx.clock.advance(seconds=0.3)
    await rig.send(_b())
    assert "Updated" in rig.sent()[-1]["text"]
    _assert_reply_in_all_then_delete(rig)


async def test_command_then_topic_deletes_the_topic_after_the_reply(ctx: AppContext, rig: TgRig) -> None:
    await rig.send(_b())
    assert "message_thread_id" not in rig.sent()[-1] and _deletes(rig) == []  # replied without waiting
    ctx.clock.advance(seconds=0.3)
    await rig.send(_a())
    _assert_reply_in_all_then_delete(rig)


@pytest.mark.parametrize("first", ["topic", "command"])
async def test_no_partner_within_the_window_deletes_nothing(ctx: AppContext, rig: TgRig, first: str) -> None:
    updates = [_a(), _b()] if first == "topic" else [_b(), _a()]
    await rig.send(updates[0])
    ctx.clock.advance(seconds=3)
    await rig.send(updates[1])
    assert _deletes(rig) == []
    assert rig.handlers.implicit.pending() == ((0, 1) if first == "topic" else (1, 0))  # only the newcomer


async def test_command_alone_deletes_nothing(ctx: AppContext, rig: TgRig) -> None:
    await rig.send(_b())
    ctx.clock.advance(seconds=3)
    await rig.say("/help", message_id=N + 5)  # the purge runs; the old command expired unpaired
    assert _deletes(rig) == []
    assert rig.handlers.implicit.pending() == (0, 1)


@pytest.mark.parametrize(("name", "mid"), [("My notes", N + 1), ("/help", N + 1), ("/status", N + 2)])
async def test_user_created_topic_is_never_deleted(ctx: AppContext, rig: TgRig, name: str, mid: int) -> None:
    await rig.send(_a(name=name))  # a topic the owner named, or no command N+1
    await rig.send(_b(mid=mid))
    assert _deletes(rig) == []


async def test_implicit_flag_pairs_even_when_the_name_differs(ctx: AppContext, rig: TgRig) -> None:
    await rig.send(_a(name="New topic", implicit=True))
    await rig.send(_b())
    _assert_reply_in_all_then_delete(rig)


async def test_command_with_the_bot_suffix_matches_the_topic_name(ctx: AppContext, rig: TgRig) -> None:
    await rig.send(_a(name="/status@ossbot_test_bot"))
    await rig.send(_b("/status"))
    _assert_reply_in_all_then_delete(rig)


@pytest.mark.parametrize("name", ["control", "reviews", "feed", "assistant"])
async def test_system_topics_are_never_deleted(ctx: AppContext, rig: TgRig, name: str) -> None:
    thread = ui_of(ctx).topics.thread_id_raw(name)
    assert thread is not None
    await rig.send(_a(thread=thread, implicit=True))
    await rig.send(_b())
    assert _deletes(rig) == []


async def test_a_thread_the_bot_posted_into_is_never_deleted(ctx: AppContext, rig: TgRig) -> None:
    ctx.outbox.enqueue("sendMessage", {"chat_id": OWNER, "message_thread_id": NEW, "text": "x"})
    await rig.flush()
    await rig.send(_a())  # a known thread: not a new one, so it never pairs
    await rig.send(_b())
    assert _deletes(rig) == []
    # the second guard: a pair that names such a thread is still refused at delete time
    rig.handlers._delete_implicit(Match(NEW, "topic_first", 300, True, False), OWNER)
    await rig.flush()
    assert _deletes(rig) == []


async def test_a_system_thread_pair_is_refused_at_delete_time(ctx: AppContext, rig: TgRig) -> None:
    thread = ui_of(ctx).topics.thread_id_raw("control")
    assert thread is not None
    rig.handlers._delete_implicit(Match(thread, "command_first", 300, True, True), OWNER)
    await rig.flush()
    assert _deletes(rig) == []


async def test_duplicate_updates_delete_once(ctx: AppContext, rig: TgRig) -> None:
    a, b = _a(), _b()
    await rig.send(a)
    await rig.send(a)
    await rig.send(b)
    await rig.send(b)
    await rig.send(_a())  # the same message again under a new update id
    assert _deletes(rig) == [NEW]


async def test_io_command_deletes_after_its_reply(ctx: AppContext, rig: TgRig) -> None:
    await rig.send(_a(name="/title x"))
    await rig.send(_b("/title x"))
    assert "Only a chat thread" in rig.sent()[-1]["text"]
    _assert_reply_in_all_then_delete(rig)


async def test_refused_delete_is_logged_once_and_the_reply_stands(
    ctx: AppContext, rig: TgRig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.req.fail("deleteForumTopic", "Bad Request: TOPIC_ID_INVALID")
    with caplog.at_level(logging.INFO):
        await rig.send(_a())
        await rig.send(_b())
        await rig.flush()
    assert "Updated" in rig.sent()[-1]["text"]
    assert _deletes(rig) == [NEW]  # one attempt, no retry; no close/hide exists in private chats
    assert "closeForumTopic" not in rig.req.methods()
    failed = [r for r in caplog.records if getattr(r, "event", None) == "stray_topic_delete_failed"]
    assert len(failed) == 1 and failed[0].thread_id == NEW


async def test_logs_carry_structure_but_never_text(
    ctx: AppContext, rig: TgRig, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "/status zebra-quokka-7731"
    with caplog.at_level(logging.INFO):
        await rig.send(_a(name=secret))
        await rig.send(_b(secret))
    fields = [r for r in caplog.records if getattr(r, "event", None) == "tg_thread_fields"]
    a, b = fields
    assert a.content_keys == ["forum_topic_created"] and b.content_keys == ["text"]
    assert a.topic_created == {"name_len": len(secret), "name_is_command": True, "icon_color": 7322096,
                               "has_icon_custom_emoji": False, "is_name_implicit": None}  # fmt: skip
    assert a.from_is_bot is False and a.from_is_owner is True and b.topic_created is None
    match = [r for r in caplog.records if getattr(r, "event", None) == "implicit_topic_match"]
    assert [(r.thread_id, r.order, r.name_matches_command, r.action) for r in match] == [
        (NEW, "topic_first", True, "delete")]  # fmt: skip
    assert [r for r in caplog.records if getattr(r, "event", None) == "stray_topic_deleted"]
    for r in caplog.records:
        assert "zebra-quokka" not in r.getMessage()
        assert "zebra-quokka" not in str(vars(r))


async def test_flat_mode_never_correlates(ctx: AppContext) -> None:
    async for flat in make_rig(ctx, topics=False):
        await flat.send(_a())
        await flat.send(_b())
        assert _deletes(flat) == []


# ------------------------------------------------------------------ the correlator alone
def _topic(mid: int = N, at: float = 0.0, **kw: Any) -> TopicSeen:
    base: dict[str, Any] = {"chat_id": 1, "user_id": 1, "message_id": mid, "thread_id": NEW, "name": "/status",
                            "implicit": False, "at": at}  # fmt: skip
    return TopicSeen(**{**base, **kw})


def _cmd(mid: int = N + 1, at: float = 0.3, **kw: Any) -> CommandSeen:
    base: dict[str, Any] = {"chat_id": 1, "user_id": 1, "message_id": mid, "text": "/status", "at": at}
    return CommandSeen(**{**base, **kw})


def test_command_key_and_name_match() -> None:
    assert command_key("  /Status@ossbot_bot   now ") == "/status now"
    assert name_matches("/status@x", "/STATUS") and not name_matches("status", "status")
    assert not name_matches("", "/status") and not name_matches("/help", "/status")


def test_pairs_only_same_chat_user_and_next_message() -> None:
    for cmd in (_cmd(chat_id=2), _cmd(user_id=2), _cmd(mid=N + 2), _cmd(at=2.5)):
        c = ImplicitTopics()
        c.on_topic(_topic())
        assert c.on_command(cmd) is None
    c = ImplicitTopics()
    assert c.on_command(_cmd()) is None
    match = c.on_topic(_topic(at=0.5))
    assert match is not None and match.order == "command_first" and match.gap_ms == 200


def test_bounded_and_replay_safe() -> None:
    c = ImplicitTopics(cap=4)
    for i in range(10):
        c.on_topic(_topic(mid=100 + i * 10, thread_id=200 + i))
    assert c.pending() == (4, 0)
    c = ImplicitTopics()
    c.on_topic(_topic())
    assert c.on_command(_cmd()) is not None
    assert c.on_command(_cmd()) is None and c.on_topic(_topic()) is None  # replays
    assert c.on_topic(_topic(mid=N + 10)) is None and c.on_command(_cmd(mid=N + 11)) is None  # thread done
    assert c.pending() == (0, 1)
