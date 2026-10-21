"""Typed text in "All" continues in its own topic session (post-v1 plan D10, ``telegram.yaml
all_text_mode: topic_session``).

Plain text typed in All arrives like a command (all-probe.md): update A is the ``forum_topic_created`` of
the topic the client just opened (message N), update B the text without a thread (message N+1). With
``topic_session`` the pair is adopted: B is answered inside A's thread with the session
``tg-<uid>-t<thread>`` and that topic is never deleted. Without A within ``TTL_S`` (or with
``all_text_mode: legacy``, flat mode, or users not allowed to create topics) B is answered in All
exactly as before. Commands typed in All keep deleting their implicit topic."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import pytest

from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.implicit_topics import ImplicitTopics, Match
from ossbot.gatekeeper.telegram.runtime import ui_of
from telegram import Update
from tests.fakes.fake_telegram import media_update, message_update, topic_created_update
from tests.telegram import golden
from tests.telegram.chatkit import FakeBackend, deltas, wire
from tests.telegram.test_work_link_cards import URL, FakeReader
from tests.telegram.tgharness import OWNER, TgRig, make_rig
from tests.unit.gatekeeper.chat import images

NEW = 125201  # the thread Telegram assigns to the implicit topic (synthetic)
N = 7000  # update A; the typed message is N + 1
TEXT = "hello there, how does the scheduler pick a repo?"
KEY = f"tg-{OWNER}-t{NEW}"
IMPLICIT = {"name": TEXT[:16] + "...", "icon_color": 7322096, "is_name_implicit": True}
SEND_METHODS = {"sendMessage", "sendMessageDraft", "sendRichMessage", "sendRichMessageDraft", "sendDocument"}


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx, topics=True, user_topics=True):
        yield r


def _legacy(ctx: AppContext) -> None:
    ctx.config.telegram.__dict__["all_text_mode"] = "legacy"  # as if telegram.yaml said legacy (frozen model)


def _a(*, thread: int = NEW, mid: int = N, name: str = TEXT[:16] + "...",
       implicit: bool | None = None) -> dict[str, Any]:  # fmt: skip
    return topic_created_update(OWNER, OWNER, thread_id=thread, message_id=mid, name=name, implicit=implicit)


def _b(text: str = TEXT, *, mid: int = N + 1) -> dict[str, Any]:
    return message_update(OWNER, OWNER, text, message_id=mid)  # no thread: typed in All


async def _process(rig: TgRig, data: dict[str, Any]) -> None:
    """One update through PTB's slot without waiting for the spawned tasks (``rig.send`` waits)."""
    await rig.app.process_update(Update.de_json(data, rig.app.bot))


def _deletes(rig: TgRig) -> list[int]:
    return [int(p["message_thread_id"]) for m, p in rig.req.calls if m == "deleteForumTopic"]


def _send_threads(rig: TgRig) -> list[int | None]:
    return [p.get("message_thread_id") for m, p in rig.req.calls if m in SEND_METHODS]


def _flat_key(ctx: AppContext) -> str:
    assert ctx.chat is not None
    return ctx.chat.service.turns.session_key(OWNER)


def _normalized(rig: TgRig) -> str:
    """The Bot API calls as JSON lines, with the random callback tokens and draft ids replaced."""
    lines = [json.dumps([m, p], sort_keys=True, ensure_ascii=False) for m, p in rig.req.calls]
    lines = [re.sub(r'"draft_id": \d+', '"draft_id": 0', line) for line in lines]
    return "\n".join(re.sub(r"t:[A-Za-z0-9_-]{22}", "t:TOKEN", line) for line in lines) + "\n"


def _assert_adopted(ctx: AppContext, rig: TgRig, backend: FakeBackend, *, turns: int = 1) -> None:
    assert [key for _, key in backend.calls] == [KEY] * turns
    threads = _send_threads(rig)
    assert threads and all(t == NEW for t in threads)
    assert _deletes(rig) == []
    assert rig.handlers.implicit.adopted(OWNER, NEW)


# ------------------------------------------------------------------ adopted
async def test_topic_first_text_is_answered_in_its_own_topic_session(ctx: AppContext, rig: TgRig) -> None:
    backend = wire(ctx, FakeBackend(deltas("It ranks targets.")))
    await rig.send(_a())
    ctx.clock.advance(seconds=0.3)
    await rig.send(_b())
    _assert_adopted(ctx, rig, backend)
    answer = rig.sent("sendRichMessage")[-1]
    assert answer["reply_parameters"] == {"message_id": N + 1, "allow_sending_without_reply": True}  # V7


async def test_text_first_is_adopted_by_the_spawned_task(ctx: AppContext, rig: TgRig) -> None:
    backend = wire(ctx, FakeBackend(deltas("ok")))
    await _process(rig, _b())  # the handler returned at once: the wait runs in a spawned task
    assert rig.handlers.implicit.pending_texts() == 1 and rig.handlers.tasks and backend.calls == []
    ctx.clock.advance(seconds=0.3)
    await rig.send(_a())
    _assert_adopted(ctx, rig, backend)
    assert rig.handlers.implicit.pending_texts() == 0


async def test_no_topic_within_the_window_is_answered_in_all(ctx: AppContext, rig: TgRig) -> None:
    backend = wire(ctx, FakeBackend(deltas("ok")))
    rig.handlers.implicit = ImplicitTopics(ttl_s=0.05)
    await rig.send(_b())
    assert [key for _, key in backend.calls] == [_flat_key(ctx)]
    assert all(t is None for t in _send_threads(rig)) and _deletes(rig) == []
    golden.check("all_text_in_all.txt", _normalized(rig))  # the same bytes as all_text_mode legacy


async def test_legacy_mode_sends_are_byte_identical_to_today(ctx: AppContext) -> None:
    _legacy(ctx)
    async for rig in make_rig(ctx, topics=True, user_topics=True):
        wire(ctx, FakeBackend(deltas("ok")))
        await rig.send(_b())
        golden.check("all_text_in_all.txt", _normalized(rig))  # recorded with the pre-FEAT-004 code


async def test_legacy_mode_never_adopts(ctx: AppContext, caplog: pytest.LogCaptureFixture) -> None:
    _legacy(ctx)
    async for rig in make_rig(ctx, topics=True, user_topics=True):
        backend = wire(ctx, FakeBackend(deltas("ok")))
        with caplog.at_level(logging.INFO):
            await rig.send(_a(implicit=True))
            await rig.send(_b())
        assert [key for _, key in backend.calls] == [_flat_key(ctx)]
        assert all(t is None for t in _send_threads(rig)) and _deletes(rig) == []
        assert rig.handlers.implicit.pending() == (1, 0) and rig.handlers.implicit.pending_texts() == 0
        assert not [r for r in caplog.records if getattr(r, "event", None) == "implicit_topic_match"]


async def test_flat_mode_is_unaffected(ctx: AppContext) -> None:
    async for rig in make_rig(ctx, topics=False, user_topics=True):
        backend = wire(ctx, FakeBackend(deltas("ok")))
        await rig.send(_a(implicit=True))
        await rig.send(_b())
        golden.check("all_text_flat.txt", _normalized(rig))  # recorded with the pre-FEAT-004 code
        assert [key for _, key in backend.calls] == [_flat_key(ctx)]
        assert rig.handlers.implicit.pending_texts() == 0


async def test_users_without_topic_rights_keep_todays_path(ctx: AppContext) -> None:
    async for rig in make_rig(ctx, topics=True, user_topics=False):  # the client creates no topic then
        backend = wire(ctx, FakeBackend(deltas("ok")))
        await rig.send(_b())
        golden.check("all_text_in_all.txt", _normalized(rig))
        assert [key for _, key in backend.calls] == [_flat_key(ctx)]
        assert rig.handlers.implicit.pending_texts() == 0


# ------------------------------------------------------------------ inside the adopted topic
async def test_a_second_message_in_the_topic_continues_the_session(ctx: AppContext, rig: TgRig) -> None:
    backend = wire(ctx, FakeBackend(deltas("ok")))
    await rig.send(_a())
    await rig.send(_b())
    await rig.say("and on Sundays?", thread_id=NEW, topic_created=IMPLICIT, message_id=N + 5)
    _assert_adopted(ctx, rig, backend, turns=2)


async def test_a_command_later_in_the_topic_is_answered_in_place(ctx: AppContext, rig: TgRig) -> None:
    wire(ctx, FakeBackend(deltas("ok")))
    await rig.send(_a())
    await rig.send(_b())
    await rig.say("/help", thread_id=NEW, topic_created=IMPLICIT, message_id=N + 5)
    assert "/status" in rig.sent()[-1]["text"] and rig.sent()[-1]["message_thread_id"] == NEW
    assert _deletes(rig) == []


async def test_a_command_typed_in_all_still_deletes_its_own_topic(ctx: AppContext, rig: TgRig) -> None:
    await rig.send(_a(name="/status"))
    await rig.send(_b("/status"))
    assert "message_thread_id" not in rig.sent()[-1]
    assert _deletes(rig) == [NEW]


async def test_an_adopted_thread_is_refused_at_delete_time(ctx: AppContext, rig: TgRig) -> None:
    wire(ctx, FakeBackend(deltas("ok")))
    await rig.send(_a())
    await rig.send(_b())
    rig.handlers._delete_implicit(Match(NEW, "topic_first", 300, True, True), OWNER)  # a command match
    rig.handlers._delete_implicit(Match(NEW, "topic_first", 300, True, True, "text"), OWNER)
    await rig.flush()
    assert _deletes(rig) == []


# ------------------------------------------------------------------ links and media
async def test_a_link_typed_in_all_gets_its_card_in_the_topic(ctx: AppContext, rig: TgRig) -> None:
    backend = wire(ctx, FakeBackend(deltas("ok")))
    rig.handlers.reader = FakeReader()
    await rig.send(_a(name=URL[:16] + "..."))
    await rig.send(_b(URL))
    cards = [p for p in rig.sent() if "🔗" in str(p.get("text", ""))]
    assert cards and all(p["message_thread_id"] == NEW for p in cards)
    assert backend.calls == [] and _deletes(rig) == []  # a link-only message is not relayed


async def test_a_photo_typed_in_all_is_handled_in_the_topic(ctx: AppContext, rig: TgRig) -> None:
    backend = wire(ctx, FakeBackend(deltas("A cat.")))
    rig.req.file_bytes["p0"] = images.jpeg()
    await rig.send(_a(name="Photo", implicit=True))
    await rig.send(media_update(OWNER, OWNER, photo="p0", message_id=N + 1))
    _assert_adopted(ctx, rig, backend)


@pytest.mark.parametrize("first", ["topic", "album"])
async def test_an_album_typed_in_all_is_one_turn_in_the_topic(ctx: AppContext, rig: TgRig, first: str) -> None:
    backend = wire(ctx, FakeBackend(deltas("Two cats.")))
    items: list[dict[str, Any]] = []
    for i in range(2):
        rig.req.file_bytes[f"q{i}"] = images.jpeg()
        items.append(media_update(OWNER, OWNER, photo=f"q{i}", media_group_id="ga", message_id=N + 1 + i))
    a = _a(name="Photo", implicit=True)
    order = [a, *items] if first == "topic" else [*items, a]
    for data in order:
        await _process(rig, data)
    await rig.settle()
    await rig.flush()
    _assert_adopted(ctx, rig, backend)
    assert sorted(rig.req.downloads) == ["q0", "q1"]


# ------------------------------------------------------------------ guards
@pytest.mark.parametrize("name", ["control", "reviews", "feed", "assistant"])
async def test_system_topics_are_never_adopted(ctx: AppContext, rig: TgRig, name: str) -> None:
    backend = wire(ctx, FakeBackend(deltas("ok")))
    rig.handlers.implicit = ImplicitTopics(ttl_s=0.05)
    thread = ui_of(ctx).topics.thread_id_raw(name)
    assert thread is not None
    await rig.send(_a(thread=thread, implicit=True))
    await rig.send(_b())
    assert [key for _, key in backend.calls] == [_flat_key(ctx)]
    assert all(t is None for t in _send_threads(rig)) and not rig.handlers.implicit.adopted(OWNER, thread)


async def test_replayed_updates_never_adopt_twice(ctx: AppContext, rig: TgRig,
                                                  caplog: pytest.LogCaptureFixture) -> None:  # fmt: skip
    backend = wire(ctx, FakeBackend(deltas("ok")))
    a, b = _a(), _b()
    with caplog.at_level(logging.INFO):
        await rig.send(a)
        await rig.send(a)
        await rig.send(b)
        await rig.send(b)
        await rig.send(_b(mid=N + 1))  # the same message under a new update id
    adopts = [r for r in caplog.records if getattr(r, "event", None) == "implicit_topic_match"]
    assert [(r.kind, r.action) for r in adopts] == [("text", "adopt")]
    _assert_adopted(ctx, rig, backend)


async def test_logs_carry_ids_and_flags_but_never_text(ctx: AppContext, rig: TgRig,
                                                       caplog: pytest.LogCaptureFixture) -> None:  # fmt: skip
    wire(ctx, FakeBackend(deltas("ok")))
    typed = "zebra-quokka-7731 tell me about otters"  # a marker that must never reach a log line
    rig.handlers.implicit = ImplicitTopics(ttl_s=0.05)
    with caplog.at_level(logging.INFO):
        await rig.send(_a(name=typed[:16] + "..."))
        await rig.send(_b(typed))  # adopted
        await rig.send(_b(typed, mid=N + 9))  # no A: the fallback path
    events = {getattr(r, "event", None) for r in caplog.records}
    assert {"implicit_topic_pending", "implicit_topic_match", "implicit_topic_expired"} <= events
    for r in caplog.records:
        assert "zebra-quokka" not in r.getMessage()
        assert "zebra-quokka" not in str(vars(r))
