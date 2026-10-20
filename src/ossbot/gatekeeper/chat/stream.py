"""One streamed Hermes turn as state (DESIGN §14.12.1, §14.12.3, §14.20.5).

``StreamState`` accumulates the text deltas (cut at ``ANSWER_CAP``), the tool items and the source
URLs of one turn; ``status_line`` is the draft-only tool activity line, built only from tool names and
domains (arguments and results are never shown). ``consume`` reads the backend's events in a child
task so a Stop (``stop`` event) closes the local SSE read at once: the child is cancelled, which closes
the HTTP stream, and Hermes interrupts the run on disconnect (§14.12.1 step 5; the runs API is not
used: Hermes v0.22.2 advertises no runs in ``/v1/capabilities``).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Literal

from ossbot.gatekeeper.chat import hermes_api
from ossbot.gatekeeper.chat.hermes_api import Event, HermesError
from ossbot.gatekeeper.telegram.ui.sanitize import domain_of

ANSWER_CAP = 200 * 1024  # §14.20.5: an answer is cut at 200 KB ("…(truncated)")
SOURCES_MAX = 8
TOOL_NAME_MAX = 32
End = Literal["completed", "stopped", "truncated"]
_LINK = re.compile(r"\]\((https?://[^)\s]{1,2048})\)|(?<![\w(\[])(https?://[^\s<>()\[\]\"'`]{1,2048})")
WEB_SEARCH = frozenset({"web_search", "search_web", "websearch"})
WEB_FETCH = frozenset({"web_fetch", "fetch", "fetch_url", "browse"})
MEMORY = frozenset({"memory", "session_search", "memory_search", "memory_write"})


@dataclass
class StreamState:
    started: float = field(default_factory=time.monotonic)
    parts: list[str] = field(default_factory=list)
    size: int = 0
    tools: list[str] = field(default_factory=list)
    fetch_urls: list[str] = field(default_factory=list)
    searches: int = 0
    current: str = ""  # the newest status line
    final: str | None = None
    response_id: str = ""
    model: str = ""
    truncated: bool = False
    deltas: int = 0

    @property
    def text(self) -> str:
        return self.final if self.final is not None else "".join(self.parts)

    @property
    def elapsed_s(self) -> int:
        return int(time.monotonic() - self.started)

    def feed(self, event: Event) -> bool:
        """Apply one event; returns False once the answer reached ``ANSWER_CAP`` (stop reading)."""
        if event.kind == "delta":
            self.deltas += 1
            room = ANSWER_CAP - self.size
            piece = event.text[:room]
            self.parts.append(piece)
            self.size += len(piece)
            if len(event.text) > room:
                self.truncated = True
                return False
        elif event.kind == "tool" and event.tool:
            self.tools.append(event.tool)
            if event.tool in WEB_SEARCH:
                self.searches += 1
            self.current = status_for(event.tool, "", self.searches)
        elif event.kind == "tool_done" and event.tool in WEB_FETCH:
            url = str(event.data.get("url") or "")
            if url and url not in self.fetch_urls:
                self.fetch_urls.append(url)
            self.current = status_for(event.tool, url, self.searches)
        elif event.kind == "completed":
            text = hermes_api.final_text(event.data)
            if text:
                self.final = text[:ANSWER_CAP]
                self.truncated = self.truncated or len(text) > ANSWER_CAP
            self.response_id = event.response_id
            model = event.data.get("model")
            self.model = model[:80] if isinstance(model, str) else ""
        elif event.kind == "failed":
            raise HermesError(event.text)
        return True

    def status_line(self) -> str:
        return self.current

    @property
    def fetches(self) -> int:
        return len(self.fetch_urls)

    def sources(self, answer: str) -> list[str]:
        """``web_fetch`` URLs, then links in the final answer; deduplicated, http(s) only, ≤ 8."""
        out: list[str] = []
        for found in [*self.fetch_urls, *(a or b for a, b in _LINK.findall(answer))]:
            url = found.rstrip(".,;:!?")
            if domain_of(url) and url not in out:
                out.append(url)
            if len(out) == SOURCES_MAX:
                break
        return out


def status_for(tool: str, url: str, searches: int) -> str:
    """§14.12.3: the one status line per tool kind (never arguments or results)."""
    if tool in WEB_SEARCH:
        return f"🔎 Searching the web ({max(1, searches)})"
    if tool in WEB_FETCH:
        host = domain_of(url) if url else None
        return f"📄 Reading {host}" if host else "📄 Reading a page"
    if tool in MEMORY:
        return "🧠 Checking memory"
    if tool.startswith("ossbot_"):
        return f"🛰 ossbot: {tool[len('ossbot_') :][:TOOL_NAME_MAX]}"
    return f"🛠 {tool[:TOOL_NAME_MAX]}"


async def _read(events: AsyncIterator[Event], state: StreamState, on_change: Callable[[], None]) -> End:
    async for event in events:
        more = state.feed(event)
        on_change()
        if not more:
            return "truncated"
    return "truncated" if state.truncated else "completed"


async def consume(events: AsyncIterator[Event], state: StreamState, stop: asyncio.Event,
                  on_change: Callable[[], None]) -> End:  # fmt: skip
    """Read ``events`` until they end, the cap is reached or ``stop`` is set. On a stop the reader is
    cancelled first (the SSE read closes at once). Errors of the stream propagate."""
    reader = asyncio.ensure_future(_read(events, state, on_change))
    stopper = asyncio.ensure_future(stop.wait())
    try:
        done, _ = await asyncio.wait({reader, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if reader in done:
            return reader.result()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        return "stopped"
    finally:
        for task in (reader, stopper):
            if not task.done():
                task.cancel()
        await asyncio.gather(reader, stopper, return_exceptions=True)
