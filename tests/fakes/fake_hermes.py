"""Fake Hermes for the assistant tests (DESIGN §14.18.5 budget tests, §15.6 backend tests).

``FakeHermesStream.deltas()`` yields text deltas and then holds the stream open for ``hold_s`` (a
long answer still streaming) unless ``stop()`` is called, like ``stopped_message_generation``.

``FakeHermesApi`` is an ``httpx.MockTransport`` for the Hermes API server at v0.22.2: ``POST
/v1/responses`` with ``stream: true`` answers the scripted SSE events (``response.output_text.delta``,
``response.output_item.added`` function calls, ``response.completed`` with the final ``output``,
``: keepalive`` comments) and ``GET /v1/toolsets`` the scripted surface. Every request is recorded.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx


class FakeHermesStream:
    def __init__(self, parts: Sequence[str], *, hold_s: float = 60.0, interval_s: float = 0.0) -> None:
        self.parts = list(parts)
        self.hold_s = hold_s
        self.interval_s = interval_s
        self.sent: list[str] = []
        self.started = asyncio.Event()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def deltas(self) -> AsyncIterator[str]:
        self.started.set()
        for part in self.parts:
            if self._stop.is_set():
                return
            if self.interval_s:
                await asyncio.sleep(self.interval_s)
            self.sent.append(part)
            yield part
        try:
            await asyncio.wait_for(self._stop.wait(), self.hold_s)
        except TimeoutError:
            return


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def completed(text: str, *, response_id: str = "resp_1", commentary: str = "") -> str:
    output: list[dict[str, Any]] = []
    if commentary:
        output.append({"type": "message", "phase": "commentary",
                       "content": [{"type": "output_text", "text": commentary}]})  # fmt: skip
    output.append({"type": "message", "content": [{"type": "output_text", "text": text}]})
    return sse("response.completed", {"type": "response.completed", "response": {"id": response_id, "output": output}})


def answer_events(deltas: Sequence[str], *, tools: Sequence[str] = (), final: str | None = None) -> list[str]:
    """A normal turn: created, tool calls, text deltas with a keepalive, completed."""
    out = [sse("response.created", {"type": "response.created", "response": {"id": "resp_1"}}), ": keepalive\n\n"]
    for name in tools:
        out.append(sse("response.output_item.added", {"type": "response.output_item.added",
                                                      "item": {"type": "function_call", "name": name}}))  # fmt: skip
    for d in deltas:
        out.append(sse("response.output_text.delta", {"type": "response.output_text.delta", "delta": d}))
    out.append(completed("".join(deltas) if final is None else final))
    return out


@dataclass
class FakeHermesApi:
    events: list[str] = field(default_factory=lambda: answer_events(["Hello", " there"]))
    status: int = 200
    toolsets: Any = None
    toolsets_status: int = 200
    connect_error: bool = False
    requests: list[dict[str, Any]] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.connect_error:
            raise httpx.ConnectError("refused", request=request)
        body = json.loads(request.content) if request.content else None
        self.requests.append({"method": request.method, "path": request.url.path, "headers": dict(request.headers),
                              "body": body})  # fmt: skip
        if request.url.path == "/v1/toolsets":
            return httpx.Response(self.toolsets_status, json=self.toolsets)
        if request.url.path != "/v1/responses":
            return httpx.Response(404, json={"error": {"message": "not found"}})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "unsupported_content_type"}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content="".join(self.events).encode())  # fmt: skip
