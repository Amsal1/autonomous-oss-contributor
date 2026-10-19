"""CR-001 item 1: long polling survives a dropped connection (IP change, Wi-Fi switch, sleep).

A real PTB Application polls a fake Bot API whose ``getUpdates`` fails twice with a network
error and then delivers a pending ``/status``: the poller reconnects with backoff, the update is
not lost, and the reconnect is counted and shown by ``/status``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from telegram.error import NetworkError
from telegram.request import RequestData

from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.outbox import OutboxWorker
from ossbot.gatekeeper.telegram import bot as bot_mod
from ossbot.gatekeeper.telegram.bot import build_application
from ossbot.gatekeeper.telegram.handlers import Handlers
from tests.fakes.fake_telegram import FakeBotRequest, message_update

OWNER = 4242


class FlakyPoll(FakeBotRequest):
    def __init__(self, failures: int, pending: list[dict[str, Any]]) -> None:
        super().__init__()
        self.failures = failures
        self.pending = pending
        self.offsets: list[Any] = []

    async def do_request(
        self,
        url: str,
        method: str,
        request_data: RequestData | None = None,
        read_timeout: Any = None,
        write_timeout: Any = None,
        connect_timeout: Any = None,
        pool_timeout: Any = None,
    ) -> tuple[int, bytes]:
        if not url.endswith("/getUpdates"):
            return await super().do_request(url, method, request_data)
        params = dict(request_data.parameters) if request_data is not None else {}
        self.offsets.append(params.get("offset"))
        if self.failures > 0:
            self.failures -= 1
            raise NetworkError("connection dropped")
        offset = int(params.get("offset") or 0)
        batch = [u for u in self.pending if u["update_id"] >= offset]
        if not batch:
            await asyncio.sleep(0.05)
        return 200, json.dumps({"ok": True, "result": batch}).encode()


class FlakyApi(FakeBotRequest):
    """getMe fails while 'offline' (no network, proxy restarting) the first ``failures`` times."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    async def do_request(self, url: str, method: str, request_data: RequestData | None = None,
                         *args: Any, **kwargs: Any) -> tuple[int, bytes]:  # fmt: skip
        if url.endswith("/getMe") and self.failures > 0:
            self.failures -= 1
            raise NetworkError("httpx.ConnectError: All connection attempts failed")
        return await super().do_request(url, method, request_data)


async def test_frontend_start_never_fails_the_gatekeeper_offline(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gatekeeper started without network (boot offline, proxy restarting) must come up; the bot
    connects later in the background (it used to fail the lifespan and crash-loop)."""
    monkeypatch.setattr(bot_mod, "CONNECT_BACKOFF_MIN_S", 0.01)
    req = FlakyApi(failures=3)
    app = build_application("123456:TEST-TOKEN-NOT-REAL", Handlers(ctx, None, None), request=req,
                            poll_request=FlakyPoll(failures=0, pending=[]))  # fmt: skip
    frontend = bot_mod.TelegramFrontend(ctx, app)
    await asyncio.wait_for(frontend.start(), timeout=1)  # returns at once, offline or not
    try:
        for _ in range(200):
            if app.updater is not None and app.updater.running:
                break
            await asyncio.sleep(0.02)
        assert app.updater is not None and app.updater.running
        assert ctx.poll_stats.as_dict()["total_failures"] >= 3
    finally:
        await frontend.stop()


async def test_poller_reconnects_and_keeps_pending_updates(ctx: AppContext) -> None:
    update = message_update(OWNER, OWNER, "/status")
    poll = FlakyPoll(failures=2, pending=[update])
    req = FakeBotRequest()
    handlers = Handlers(ctx, None, None)
    app = build_application("123456:TEST-TOKEN-NOT-REAL", handlers, request=req, poll_request=poll)
    await app.initialize()
    await app.start()
    assert app.updater is not None
    await app.updater.start_polling(poll_interval=0.0, timeout=0, drop_pending_updates=False)
    worker = OutboxWorker(ctx.outbox, bot_mod.TelegramSender(app.bot), batch=20)  # replies go through the outbox
    try:
        for _ in range(300):
            await worker.run_once()
            if "sendMessage" in req.methods():
                break
            await asyncio.sleep(0.05)
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
    replies = [p["text"] for m, p in req.calls if m == "sendMessage"]
    assert replies, "the pending /status update was lost"
    assert "reconnects 1" in replies[0]
    stats = ctx.poll_stats.as_dict()
    assert stats["reconnects"] == 1 and stats["total_failures"] == 2 and stats["consecutive_failures"] == 0
    assert stats["last_ok_at"] is not None and "NetworkError" in (stats["last_error"] or "")
    # the offset only advanced after the update was fetched: nothing skipped across the outage
    assert poll.offsets[0] in (None, 0)
    assert update["update_id"] + 1 in poll.offsets


class RecordingPoll(FlakyPoll):
    def __init__(self, pending: list[dict[str, Any]]) -> None:
        super().__init__(failures=0, pending=pending)
        self.allowed: list[Any] = []

    async def do_request(self, url: str, method: str, request_data: RequestData | None = None,
                         *args: Any, **kwargs: Any) -> tuple[int, bytes]:  # fmt: skip
        if url.endswith("/getUpdates") and request_data is not None:
            self.allowed.append(dict(request_data.parameters).get("allowed_updates"))
        return await super().do_request(url, method, request_data)


async def test_polling_requests_stopped_message_generation(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """§14.2 (F009-R1-10): the poller asks for the 10.3 update and routing rule 2 receives it."""
    stopped = {"update_id": 777001, "stopped_message_generation": {"chat": {"id": OWNER, "type": "private"},
                                                                    "draft_id": 42}}  # fmt: skip
    poll = RecordingPoll([stopped])
    handlers = Handlers(ctx, None, None)
    seen: list[Any] = []
    original = handlers.on_raw

    async def on_raw(update: Any, context: Any) -> None:
        seen.append(update.api_kwargs.get("stopped_message_generation"))
        await original(update, context)

    monkeypatch.setattr(handlers, "on_raw", on_raw)
    app = build_application("123456:TEST-TOKEN-NOT-REAL", handlers, request=FakeBotRequest(), poll_request=poll)
    await app.initialize()
    await app.start()
    assert app.updater is not None
    await app.updater.start_polling(poll_interval=0.0, timeout=0, allowed_updates=bot_mod.ALLOWED_UPDATES,
                                    drop_pending_updates=False)  # fmt: skip
    try:
        for _ in range(200):
            if seen:
                break
            await asyncio.sleep(0.02)
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
    assert "stopped_message_generation" in bot_mod.ALLOWED_UPDATES
    assert any(a and "stopped_message_generation" in json.dumps(a) for a in poll.allowed)
    assert seen and seen[0]["draft_id"] == 42
