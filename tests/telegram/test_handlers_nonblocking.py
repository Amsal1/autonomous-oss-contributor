"""Update handler budget (DESIGN §14.18.5, AC 15, 59, 67; BA-2, BA-15, BA-18).

PTB processes one update at a time; these tests feed updates the same way (``process_update`` in
order) while long work runs as spawned tasks: a 60 s fake Hermes stream, a slow preflight, an ``io``
command held by a fake root helper.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from telegram.ext import Application

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import handlers as handlers_mod
from ossbot.gatekeeper.telegram.commands import registry
from ossbot.gatekeeper.telegram.commands.registry import CommandContext, Reply
from ossbot.gatekeeper.telegram.handlers import KILL_CANCELS, Handlers
from telegram import Update
from tests.conftest import packaged_contribution
from tests.fakes.fake_hermes import FakeHermesStream
from tests.fakes.fake_telegram import FakeBotRequest, callback_update, message_update, stopped_update
from tests.telegram.chatkit import FakeBackend, deltas, wire

OWNER = 4242
CARD_MSG = 50
SESSION = f"tg-{OWNER}-1"


class Rig:
    def __init__(self, app: Application[Any, Any, Any, Any, Any, Any], handlers: Handlers) -> None:
        self.app = app
        self.handlers = handlers

    async def update(self, data: dict[str, Any]) -> float:
        """Process one update like PTB's single slot; returns the handler time in seconds."""
        started = time.monotonic()
        await self.app.process_update(Update.de_json(data, self.app.bot))
        return time.monotonic() - started

    async def say(self, text: str) -> float:
        return await self.update(message_update(OWNER, OWNER, text, "private"))

    async def tap(self, token: str, message: int = CARD_MSG) -> float:
        return await self.update(callback_update(OWNER, OWNER, message, f"t:{token}", "private"))


@pytest.fixture
def preflight_s() -> list[float]:
    return [0.0]


@pytest.fixture
async def rig(ctx: AppContext, preflight_s: list[float]) -> AsyncIterator[Rig]:
    async def submit(approval_id: str) -> str:
        return "dry_run_done"

    async def preflight(approval_id: str) -> list[str]:
        await asyncio.sleep(preflight_s[0])
        return []

    handlers = Handlers(ctx, submit, preflight)
    app = Application.builder().token("123456:TEST-TOKEN-NOT-REAL").request(FakeBotRequest()).updater(None).build()
    handlers.register(app)
    await app.initialize()
    yield Rig(app, handlers)
    await handlers.drain()
    await app.shutdown()


@pytest.fixture
def no_slow_handlers(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    caplog.set_level(logging.WARNING)
    yield
    slow = [r for r in caplog.records if getattr(r, "event", None) == "handler_slow"]
    assert slow == [], [r.getMessage() for r in slow]


def _card(ctx: AppContext, message_id: int = CARD_MSG) -> tuple[str, dict[str, str]]:
    _, action = packaged_contribution(ctx)
    approval = ctx.create_approval(action, "test")
    ctx.approvals.set_card_message(approval, message_id)
    for _ in range(ctx.approvals.part_count(approval)):
        ctx.approvals.mark_part_delivered(approval)
    return approval, ctx.approvals.issue_card_tokens(approval)


def _turn(rig: Rig, stream: FakeHermesStream, outcomes: list[str]) -> asyncio.Task[Any]:
    async def consume() -> None:
        async for _ in stream.deltas():
            pass

    def finalize(task: asyncio.Task[Any]) -> None:  # stands in for chat/turns.finalize_turn (FEAT-007)
        outcomes.append("stopped" if task.cancelled() else "failed" if task.exception() else "done")

    return rig.handlers.spawn(consume(), "chat_turn", session_key=SESSION, on_done=finalize)


async def test_kill_is_handled_within_2s_while_a_60s_stream_is_open(ctx: AppContext, rig: Rig,
                                                                     no_slow_handlers: None) -> None:  # fmt: skip
    stream = FakeHermesStream(["Hello ", "world"], hold_s=60.0)
    outcomes: list[str] = []
    task = _turn(rig, stream, outcomes)
    await stream.started.wait()
    assert rig.handlers.session_busy(SESSION)
    elapsed = await rig.say("/kill")
    assert elapsed < 2.0
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)  # done callbacks run on the next loop step
    assert task.cancelled() and outcomes == ["stopped"]
    assert not rig.handlers.session_busy(SESSION)
    assert ctx.control.view().state == "killed"


async def test_approve_on_another_card_is_answered_within_2s(ctx: AppContext, rig: Rig,
                                                              no_slow_handlers: None) -> None:  # fmt: skip
    stream = FakeHermesStream(["streaming…"], hold_s=60.0)
    task = _turn(rig, stream, [])
    await stream.started.wait()
    approval, tokens = _card(ctx)
    assert await rig.tap(tokens["approve"]) < 2.0
    assert db.one(ctx.conn, "SELECT status FROM approvals WHERE id = ?", (approval,))["status"] == "armed"  # type: ignore[index]
    assert not task.done()
    stream.stop()
    await task


async def test_slow_preflight_within_its_threshold_logs_nothing(
    ctx: AppContext, rig: Rig, preflight_s: list[float], monkeypatch: pytest.MonkeyPatch, no_slow_handlers: None
) -> None:
    monkeypatch.setattr(handlers_mod, "PREFLIGHT_TIMEOUT_S", 0.6)  # threshold 1.6 s for the Confirm handler
    preflight_s[0] = 0.4
    approval, tokens = _card(ctx)
    await rig.tap(tokens["approve"])
    confirm = db.one(ctx.conn, "SELECT token FROM ui_tokens WHERE approval_id = ? AND purpose = 'confirm'",
                     (approval,))["token"]  # type: ignore[index]  # fmt: skip
    elapsed = await rig.tap(confirm)
    assert 0.4 <= elapsed < 1.6
    assert db.one(ctx.conn, "SELECT status FROM approvals WHERE id = ?", (approval,))["status"] in {  # type: ignore[index]
        "confirmed",
        "consumed",
    }


async def test_preflight_timeout_is_a_failed_live_check(
    ctx: AppContext, rig: Rig, preflight_s: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handlers_mod, "PREFLIGHT_TIMEOUT_S", 0.2)
    preflight_s[0] = 5.0
    approval, tokens = _card(ctx)
    await rig.tap(tokens["approve"])
    confirm = db.one(ctx.conn, "SELECT token FROM ui_tokens WHERE approval_id = ? AND purpose = 'confirm'",
                     (approval,))["token"]  # type: ignore[index]  # fmt: skip
    assert await rig.tap(confirm) < 1.0
    row = db.one(ctx.conn, "SELECT status, invalidated_reason FROM approvals WHERE id = ?", (approval,))
    assert row is not None and tuple(row) == ("invalidated", "live_check:timeout")


async def test_kill_during_a_slow_preflight_waits_at_most_the_preflight_bound(
    ctx: AppContext, rig: Rig, preflight_s: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review F005-R1-7 / D14: PTB's single update slot holds a typed /kill behind a Confirm whose
    preflight is still running; the wait is bounded by PREFLIGHT_TIMEOUT_S, then /kill runs within
    its 2 s budget and nothing is submitted."""
    monkeypatch.setattr(handlers_mod, "PREFLIGHT_TIMEOUT_S", 0.5)
    preflight_s[0] = 30.0  # a hung live check
    approval, tokens = _card(ctx)
    await rig.tap(tokens["approve"])
    confirm = db.one(ctx.conn, "SELECT token FROM ui_tokens WHERE approval_id = ? AND purpose = 'confirm'",
                     (approval,))["token"]  # type: ignore[index]  # fmt: skip
    slot = asyncio.Lock()  # FIFO, like PTB processing one update at a time

    async def in_slot(update: dict[str, Any]) -> float:
        async with slot:
            return await rig.update(update)

    confirm_task = asyncio.create_task(in_slot(callback_update(OWNER, OWNER, CARD_MSG, f"t:{confirm}", "private")))
    await asyncio.sleep(0.05)  # the Confirm handler holds the slot, its preflight is running
    arrived = time.monotonic()
    await in_slot(message_update(OWNER, OWNER, "/kill", "private"))
    waited = time.monotonic() - arrived
    assert waited < handlers_mod.PREFLIGHT_TIMEOUT_S + 2.0
    await confirm_task
    assert ctx.control.view().state == "killed"
    row = db.one(ctx.conn, "SELECT status FROM approvals WHERE id = ?", (approval,))
    assert row is not None and row["status"] == "invalidated"


async def test_slow_local_handler_logs_handler_slow(ctx: AppContext, rig: Rig, caplog: pytest.LogCaptureFixture,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    monkeypatch.setattr(handlers_mod, "HANDLER_SLOW_S", 0.0)  # every handler is over a zero budget
    caplog.set_level(logging.WARNING)
    await rig.say("/status")
    slow = [r for r in caplog.records if getattr(r, "event", None) == "handler_slow"]
    assert slow and getattr(slow[0], "handler", "") == "message" and "ms" in slow[0].__dict__


async def test_cancel_before_start_runs_the_done_callback(ctx: AppContext, rig: Rig) -> None:
    """A message and /kill in one getUpdates batch: the turn is cancelled before its body ran."""
    stream = FakeHermesStream(["never sent"], hold_s=60.0)
    outcomes: list[str] = []
    task = _turn(rig, stream, outcomes)  # spawned, not started (no await since)
    await rig.say("/kill")
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)  # done callbacks run on the next loop step
    assert task.cancelled() and outcomes == ["stopped"] and stream.sent == []
    assert not rig.handlers.session_busy(SESSION) and not rig.handlers.tasks


async def test_io_command_does_not_hold_the_slot_and_survives_kill(ctx: AppContext, rig: Rig,
                                                                   no_slow_handlers: None) -> None:  # fmt: skip
    release = asyncio.Event()
    done: list[str] = []

    async def held(cctx: CommandContext, args: list[str]) -> Reply:
        await asyncio.wait_for(release.wait(), 5.0)  # a fake root helper held open
        done.append("effect")
        return Reply("done")

    registry.COMMANDS["heldtest"] = registry.CommandSpec("heldtest", "test", "config", held, "io")
    try:
        assert await rig.say("/heldtest") < 1.0
        [task] = list(rig.handlers.tasks)
        assert await rig.say("/kill") < 2.0
        assert not task.cancelled()  # command tasks keep their Phase 1 kill rules
        release.set()
        await task
        assert done == ["effect"]
    finally:
        del registry.COMMANDS["heldtest"]


# ------------------------------------------------------- real chat turns (AC 59 Stop, AC 67, BA-15)
def _status(ctx: AppContext) -> list[str]:
    return [str(r["status"]) for r in ctx.conn.execute("SELECT status FROM chat_turns")]


async def test_stop_during_a_real_stream_ends_it_within_2s(ctx: AppContext, rig: Rig,
                                                            no_slow_handlers: None) -> None:  # fmt: skip
    backend = wire(ctx, FakeBackend(deltas("a long answer "), hold=True))
    await rig.say("explain")
    await backend.started.wait()
    turn_id = int(db.one(ctx.conn, "SELECT turn_id FROM chat_turns")["turn_id"])  # type: ignore[index]
    started = time.monotonic()
    assert await rig.update(stopped_update(OWNER, turn_id)) < 2.0
    await asyncio.wait_for(backend.closed.wait(), 2.0)
    await rig.handlers.drain()
    assert time.monotonic() - started < 2.0 and _status(ctx) == ["stopped"]


async def test_message_and_kill_in_one_batch_then_resume_answers_the_next_message(ctx: AppContext, rig: Rig) -> None:
    """AC 67 / BA-15 on real rows: finalize_turn is the chat turn's on_done, so a turn cancelled before
    its first step still ends ``stopped``; after /resume the session is free and the next one is answered."""
    backend = wire(ctx, FakeBackend(deltas("answer")))
    await rig.say("first")  # spawned, not started: no await since
    await rig.say("/kill")
    await asyncio.sleep(0.05)
    assert _status(ctx) == ["stopped"] and backend.calls == [] and not rig.handlers.tasks
    ctx.control.request_resume("tg:4242")
    for flag in (ctx.paths.killed_flag, ctx.paths.paused_flag, ctx.paths.resume_request):
        flag.unlink(missing_ok=True)  # what the root enforcer does
    ctx.control.reconcile()
    await rig.say("second")
    while rig.handlers.tasks:
        await asyncio.gather(*list(rig.handlers.tasks), return_exceptions=True)
        await asyncio.sleep(0)
    assert sorted(_status(ctx)) == ["done", "stopped"] and len(backend.calls) == 1


async def test_an_older_tasks_callback_leaves_the_newer_session_entry(ctx: AppContext, rig: Rig) -> None:
    release = asyncio.Event()
    task_old = rig.handlers.spawn(release.wait(), "chat_turn", session_key=SESSION)
    newer = rig.handlers.spawn(asyncio.sleep(3600), "chat_turn", session_key=SESSION)  # replaced the entry
    release.set()
    await task_old
    await asyncio.sleep(0)
    assert rig.handlers.session_busy(SESSION)  # still the newer task's
    newer.cancel()
    await asyncio.gather(newer, return_exceptions=True)
    await asyncio.sleep(0)
    assert not rig.handlers.session_busy(SESSION)


# Calls that leave SQLite/local reads: root helpers, the lifecycle and Hermes clients, the proxy
# renderer, the model writer (the /status lifecycle *display* is a local read).
IO_MARKERS = ("_root_helper", "subprocess", "httpx", "uds_client", "/opt/ossbot/bin", "render_squid", "set_model",
              "hermes_client", "lifecycle_client")  # fmt: skip


def test_registry_tags_io_effects() -> None:
    """BA-18: a command or confirm handler whose effect reaches a root helper, the lifecycle client,
    Hermes, the proxy renderer or the model writer is tagged ``io`` (so it is spawned)."""
    import inspect

    for spec in registry.COMMANDS.values():
        src = inspect.getsource(spec.func)
        if any(m in src for m in IO_MARKERS):
            assert spec.effect == "io", spec.name
    for purpose, func in registry.CONFIRMS.items():
        src = inspect.getsource(func)
        if any(m in src for m in IO_MARKERS):
            assert registry.CONFIRM_EFFECTS.get(purpose) == "io", purpose


def test_handlers_call_no_network_client_directly() -> None:
    """BA-2: handlers.py reaches httpx, Hermes, getFile and the GitHub read client only through spawn."""
    import ast

    tree = ast.parse(inspect_source(handlers_mod))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert not {m for m in imported if m and (m.startswith("httpx") or "github_read" in m or "chat" in m)}
    assert "get_file" not in inspect_source(handlers_mod) and "getFile" not in inspect_source(handlers_mod)


def inspect_source(module: Any) -> str:
    import inspect

    return inspect.getsource(module)


def test_kill_cancels_set() -> None:
    assert frozenset({"chat_turn", "media", "telegraph", "github_read", "render"}) == KILL_CANCELS
    assert handlers_mod.PREFLIGHT_TIMEOUT_S == 8.0 and handlers_mod.HANDLER_SLOW_S == 1.0
