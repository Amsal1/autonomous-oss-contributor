"""Assistant-turn helpers for the Telegram tests (DESIGN §14.12): a scripted ``ChatBackend``.

``FakeBackend`` yields scripted ``hermes_api.Event``s; with ``hold=True`` it then keeps the stream open
(a long answer still arriving) until the consuming task is cancelled, which is how a Stop or a kill
closes the SSE read. ``gate`` can be closed to simulate an unavailable assistant.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

from ossbot.gatekeeper.chat.backend import GateResult
from ossbot.gatekeeper.chat.drafts import DraftLane
from ossbot.gatekeeper.chat.hermes_api import Event
from ossbot.gatekeeper.context import AppContext


def deltas(*parts: str, tools: Sequence[str] = (), final: str | None = None, model: str = "") -> list[Event]:
    out = [Event("tool", tool=t) for t in tools]
    out += [Event("delta", text=p) for p in parts]
    data: dict[str, Any] = {"output": [{"type": "message", "content": [
        {"type": "output_text", "text": "".join(parts) if final is None else final}]}]}  # fmt: skip
    if model:
        data["model"] = model
    out.append(Event("completed", response_id="resp_1", data=data))
    return out


class FakeBackend:
    def __init__(self, events: list[Event] | None = None, *, hold: bool = False, gate: GateResult | None = None,
                 interval_s: float = 0.0) -> None:  # fmt: skip
        self.events = events if events is not None else deltas("Hello", " there")
        self.hold = hold
        self._gate = gate or GateResult(True, "ok")
        self.interval_s = interval_s
        self.calls: list[tuple[list[dict[str, Any]], str]] = []
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def gate(self) -> GateResult:
        return self._gate

    async def stream(self, parts: list[dict[str, Any]], session_key: str, *, request_role: str | None = None
                     ) -> AsyncIterator[Event]:  # fmt: skip
        self.calls.append((parts, session_key))
        self.started.set()
        try:
            for event in self.events:
                if self.hold and event.kind == "completed":
                    break
                if self.interval_s:
                    await asyncio.sleep(self.interval_s)
                yield event
            if self.hold:
                await asyncio.sleep(3600)
        finally:
            self.closed.set()


def wire(ctx: AppContext, backend: FakeBackend) -> FakeBackend:
    assert ctx.chat is not None
    ctx.chat.service.backend = backend  # type: ignore[assignment]
    DraftLane._last_by_chat.clear()  # the per-chat draft rate is process-wide; every test starts fresh
    return backend
