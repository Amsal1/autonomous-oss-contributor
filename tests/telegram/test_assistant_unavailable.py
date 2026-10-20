"""Assistant-unavailable states (DESIGN §14.16, AC 22, AC 33): the reason plus Retry within 2 s, OSS
commands keep working, and a Retry tap relays the same text again."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import pytest

from ossbot.gatekeeper.chat.backend import REPLIES, GateResult
from ossbot.gatekeeper.chat.hermes_api import HermesUnavailable
from ossbot.gatekeeper.context import AppContext
from tests.telegram.chatkit import FakeBackend, deltas, wire
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


class DownBackend(FakeBackend):
    async def stream(self, parts, session_key, *, request_role=None, instructions=None):  # type: ignore[no-untyped-def,override]
        self.calls.append((parts, session_key))
        raise HermesUnavailable("ConnectError")
        yield  # pragma: no cover - makes this an async generator


@pytest.mark.parametrize("reason", ["hermes_unavailable", "workers_down", "probe_pending", "paused"])
async def test_a_closed_relay_answers_with_its_reason_and_retry(rig: TgRig, reason: str) -> None:
    wire(rig.ctx, FakeBackend(gate=GateResult(False, reason)))  # type: ignore[arg-type]
    started = time.monotonic()
    await rig.say("hello?")
    assert time.monotonic() - started < 2.0
    payload = rig.sent()[-1]
    assert REPLIES[reason] in payload["text"]
    assert rig.buttons(payload)[0][0]["text"] == "🔁 Retry"
    row = rig.ctx.conn.execute("SELECT status FROM chat_turns").fetchone()
    assert row["status"] == "failed"


async def test_hermes_down_mid_turn_gets_unavailable_and_commands_still_work(rig: TgRig) -> None:
    wire(rig.ctx, DownBackend())
    await rig.say("hello?")
    assert "🤖 Assistant unavailable — try again." in rig.last_text()
    await rig.say("/status")
    assert rig.sent()[-1]["text"] != rig.sent()[-2]["text"]


async def test_retry_relays_the_same_text(rig: TgRig) -> None:
    wire(rig.ctx, FakeBackend(gate=GateResult(False, "hermes_unavailable")))
    await rig.say("what now?")
    payload = rig.sent()[-1]
    token = rig.token_of(payload, "🔁 Retry")
    mid = rig.message_id_of(-1)
    backend = wire(rig.ctx, FakeBackend(deltas("Now it works.")))
    await rig.tap(token, message=mid)
    assert backend.calls[0][0] == [{"type": "input_text", "text": "what now?"}]
    origins = {r["origin"] for r in rig.ctx.conn.execute("SELECT origin FROM chat_turns").fetchall()}
    assert "retry" in origins
