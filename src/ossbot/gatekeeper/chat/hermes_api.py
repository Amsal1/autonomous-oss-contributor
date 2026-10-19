"""Hermes API-server client (DESIGN §14.12, §15.6; ADR-0002 addendum FEAT-007 item 33).

``POST /v1/responses`` with ``stream: true`` and ``X-Hermes-Session-Key``; the SSE stream carries
``response.output_text.delta``, ``response.output_item.added|done`` (``function_call`` items: tool use),
``response.completed`` and ``: keepalive`` comments. Every event is untrusted data: only the documented
fields are read, sizes are bounded, and the text is redacted by the caller before it is shown or stored.
Cancel = close the stream (Hermes interrupts the run on disconnect).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

DEFAULT_URL = "http://127.0.0.1:8642"
MAX_EVENT = 1024 * 1024
MAX_ANSWER = 512 * 1024
SESSION_KEY_MAX = 256


class HermesUnavailable(RuntimeError):
    """Connection refused or timed out: chat is unavailable, nothing is at risk."""


class HermesError(RuntimeError):
    """Hermes answered with an error or an unreadable stream."""


@dataclass
class Event:
    kind: Literal["delta", "tool", "completed", "failed"]
    text: str = ""
    tool: str = ""
    response_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def _session_key(key: str) -> str:
    if not key or len(key) > SESSION_KEY_MAX or any(c in key for c in "\r\n\x00"):
        raise ValueError("bad session key")
    return key


def _obj(doc: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = doc.get(key)
    return value if isinstance(value, dict) else None


def parse_event(name: str, data: str) -> Event | None:
    """One SSE event -> an ``Event`` (unknown types are ignored)."""
    try:
        doc = json.loads(data)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    kind = str(doc.get("type") or name)
    if kind == "response.output_text.delta":
        delta = doc.get("delta")
        return Event("delta", text=delta if isinstance(delta, str) else "")
    if kind in ("response.output_item.added", "response.output_item.done"):
        item = _obj(doc, "item") or {}
        if item.get("type") == "function_call" and kind.endswith("added"):
            return Event("tool", tool=str(item.get("name") or "")[:64])
        return None
    if kind == "response.completed":
        resp = _obj(doc, "response") or {}
        return Event("completed", response_id=str(resp.get("id") or "")[:80], data=resp)
    if kind in ("response.failed", "response.cancelled", "error"):
        resp = _obj(doc, "response") or doc
        err = _obj(resp, "error") or {}
        return Event("failed", text=str(err.get("message") or kind)[:300])
    return None


def final_text(response: dict[str, Any]) -> str:
    """The answer from a ``response.completed`` payload: ``message`` items, skipping ``commentary``."""
    out: list[str] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message" or item.get("phase") == "commentary":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                out.append(part["text"])
    return "".join(out)[:MAX_ANSWER]


async def bounded_lines(chunks: AsyncIterator[bytes], limit: int) -> AsyncIterator[str]:
    """SSE lines (``\\n``, ``\\r\\n`` or ``\\r`` endings) from a byte stream; a line still unterminated after
    ``limit`` bytes raises, so a peer can never make the gatekeeper buffer more than that."""
    buf = b""
    async for chunk in chunks:
        buf += chunk
        while True:
            cut = min((i for i in (buf.find(b"\n"), buf.find(b"\r")) if i >= 0), default=-1)
            if cut < 0:
                break
            skip = 1
            if buf[cut] == 0x0D:  # \r
                if cut + 1 == len(buf):
                    break  # may be the first half of \r\n: wait for the next chunk
                skip = 2 if buf[cut + 1] == 0x0A else 1
            line, buf = buf[:cut], buf[cut + skip :]
            yield line.decode("utf-8", "replace")
        if len(buf) > limit:
            raise HermesError("SSE line over the size limit")
    if buf:
        yield buf.rstrip(b"\r").decode("utf-8", "replace")


class HermesClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_URL, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 900.0) -> None:  # fmt: skip
        self.api_key, self.base_url = api_key, base_url
        self._transport = transport
        self._timeout = httpx.Timeout(timeout, connect=5.0)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=self._timeout, trust_env=False,
                                 transport=self._transport)  # fmt: skip

    async def stream(self, parts: list[dict[str, Any]], session_key: str, *, model: str | None = None,
                     provider: str | None = None, instructions: str | None = None) -> AsyncIterator[Event]:  # fmt: skip
        body: dict[str, Any] = {"input": [{"role": "user", "content": parts}], "stream": True, "store": True,
                                "conversation": _session_key(session_key)}  # fmt: skip
        if model:
            body["model"] = model
        if provider:
            body["provider"] = provider
        if instructions:
            body["instructions"] = instructions
        headers = {"Authorization": f"Bearer {self.api_key}", "X-Hermes-Session-Key": session_key,
                   "Accept": "text/event-stream"}  # fmt: skip
        try:
            async with (
                self._client() as client,
                client.stream("POST", "/v1/responses", json=body, headers=headers) as resp,
            ):
                if resp.status_code != 200:
                    text = (await resp.aread())[:500].decode("utf-8", "replace")
                    raise HermesError(f"HTTP {resp.status_code}: {text}")
                async for event in self._events(resp):
                    yield event
        except httpx.TransportError as exc:
            # Not reachable (connect errors) or gone mid-stream (Hermes killed or restarted: a peer that
            # closes the chunked body, a read error): the turn fails and the relay gate re-checks the
            # surface before the next one (PLAN item 42, test_crash_safety.sh Hermes case).
            raise HermesUnavailable(type(exc).__name__) from exc

    @staticmethod
    async def _events(resp: httpx.Response) -> AsyncIterator[Event]:
        name, size = "", 0
        data: list[str] = []
        async for line in bounded_lines(resp.aiter_bytes(), MAX_EVENT):
            if line.startswith(":"):
                continue  # keepalive comment
            if not line:
                if data:
                    event = parse_event(name, "\n".join(data))
                    if event is not None:
                        yield event
                name, data, size = "", [], 0
                continue
            size += len(line)
            if size > MAX_EVENT:
                raise HermesError("SSE event over the size limit")
            field_name, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field_name == "event":
                name = value
            elif field_name == "data":
                data.append(value)
        if data:
            event = parse_event(name, "\n".join(data))
            if event is not None:
                yield event
