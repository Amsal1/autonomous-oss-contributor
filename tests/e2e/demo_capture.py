"""Recording helpers for the README demo capture (``tests/e2e/test_demo_capture.py``, run by
``scripts/dev/make-demo.ps1``).

``CaptureBotRequest`` is the fake Bot API of ``tests/fakes/fake_telegram.py`` that also keeps a
transcript that is never cleared: every Bot API call the production ``TelegramSender``, the handlers
and the draft lane make, with its exact parameters and the fake's answer. ``Recorder`` adds the
owner's own messages (from the real update payloads) and the scene marks. The demo renderer
(``scripts/dev/demo/demokit``) draws only what is in this transcript.

The fixed clocks and ids (``FakeTime``, ``FakeSecrets``, ``FakeMono``) make two captures byte-identical.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from ossbot.common.clock import FakeClock, to_iso
from ossbot.gatekeeper.chat.hermes_api import Event
from tests.fakes.fake_github import FakeGitHub
from tests.fakes.fake_telegram import FakeBotRequest, message_update
from tests.telegram.chatkit import FakeBackend
from tests.telegram.tgharness import OWNER

TRANSCRIPT_VERSION = 1
DRAFT_METHODS = frozenset({"sendMessageDraft", "sendRichMessageDraft"})


def _plain(value: Any) -> Any:
    """JSON-compatible copy (``RequestData.parameters`` may hold JSON strings for nested objects)."""
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


class CaptureBotRequest(FakeBotRequest):
    """``FakeBotRequest`` plus an append-only transcript of ``{type: bot, seq, at, method, params, ok, result}``."""

    def __init__(self, clock: FakeClock, **kw: Any) -> None:
        super().__init__(**kw)
        self.clock = clock
        self.events: list[dict[str, Any]] = []

    def drafts(self) -> int:
        return sum(1 for e in self.events if e["type"] == "bot" and e["method"] in DRAFT_METHODS)

    async def do_request(self, url: str, method: str, request_data: Any = None, read_timeout: Any = None,
                         write_timeout: Any = None, connect_timeout: Any = None,
                         pool_timeout: Any = None) -> tuple[int, bytes]:  # fmt: skip
        n_calls = len(self.calls)
        status, body = await super().do_request(url, method, request_data, read_timeout, write_timeout,
                                                connect_timeout, pool_timeout)  # fmt: skip
        if len(self.calls) == n_calls:  # a file download, not a Bot API call
            return status, body
        endpoint, params = self.calls[-1]
        doc = json.loads(body)
        self.events.append({"type": "bot", "seq": len(self.events) + 1, "at": to_iso(self.clock.now()),
                            "method": endpoint, "params": _plain(params), "ok": bool(doc.get("ok")),
                            "result": _plain(doc.get("result"))})  # fmt: skip
        return status, body


class Recorder:
    """The owner's messages and the scene marks, in the same event list as the Bot API calls."""

    def __init__(self, req: CaptureBotRequest) -> None:
        self.req = req
        self._ids = itertools.count(500)

    def _append(self, event: dict[str, Any]) -> None:
        self.req.events.append({"seq": len(self.req.events) + 1, "at": to_iso(self.req.clock.now()), **event})

    def user(self, text: str, *, thread_id: int | None = None) -> tuple[int, dict[str, Any]]:
        """A message update typed by the owner; the recorded bubble is the update's own text."""
        update = message_update(OWNER, OWNER, text, thread_id=thread_id, message_id=next(self._ids))
        message = update["message"]
        self._append({"type": "user", "message_id": int(message["message_id"]),
                      "thread": message.get("message_thread_id"), "text": str(message["text"])})  # fmt: skip
        return int(message["message_id"]), update

    def mark(self, name: str, show: list[int], *, pressed: str | None = None, upto: int | None = None,
             draft: int | None = None) -> None:  # fmt: skip
        """A frame: the messages ``show`` (in order) as the chat stood after event ``upto`` (default: now);
        ``draft``: also the streaming draft with that ``draft_id``; ``pressed``: the button shown tapped."""
        self._append({"type": "mark", "name": name, "show": list(show), "pressed": pressed, "upto": upto,
                      "draft": draft})  # fmt: skip

    def write(self, path: Path) -> None:
        doc = {"version": TRANSCRIPT_VERSION, "events": self.req.events}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8"))


class DemoReader:
    """The status card's GitHub reads (shape of ``Reader`` in ``tests/telegram/test_status_card.py``):
    the PR from the fake GitHub's state, scripted check runs, no reviews or comments."""

    CHECKS = ({"name": "build", "status": "completed", "conclusion": "success"},
              {"name": "tests", "status": "completed", "conclusion": "success"},
              {"name": "lint", "status": "in_progress"})  # fmt: skip

    def __init__(self, gh: FakeGitHub) -> None:
        self.gh = gh

    async def pull(self, upstream: str, number: int) -> dict[str, Any]:
        return next(p for p in self.gh.pulls[upstream.lower()] if p["number"] == number)

    async def check_runs(self, upstream: str, sha: str) -> list[dict[str, Any]]:
        return [dict(c) for c in self.CHECKS]

    async def reviews(self, upstream: str, number: int) -> list[dict[str, Any]]:
        return []

    async def comments_since(self, upstream: str, number: int, since: str) -> list[dict[str, Any]]:
        return []


class FakeTime:
    """``time`` for ``ossbot.common.ids``: the fake clock's epoch plus 1 ms per call (unique, ordered ids)."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.n = 0

    def time(self) -> float:
        self.n += 1
        return self.clock.now().timestamp() + 0.001 * self.n


class FakeSecrets:
    """``secrets`` with counters instead of entropy (demo capture only)."""

    def __init__(self) -> None:
        self.n = 0

    def _next(self) -> int:
        self.n += 1
        return self.n

    def randbelow(self, upper: int) -> int:
        return self._next() % upper

    def randbits(self, k: int) -> int:
        return (0x5EED_0000 + self._next()) & ((1 << k) - 1)

    def token_urlsafe(self, nbytes: int = 16) -> str:
        raw = self._next().to_bytes(nbytes, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    def token_hex(self, nbytes: int = 16) -> str:
        return self._next().to_bytes(nbytes, "big").hex()


class FakeMono:
    """``time.monotonic`` for the draft lane: ``step`` seconds per call, so every new delta is due."""

    def __init__(self, step: float = 2.0) -> None:
        self.t = 1000.0
        self.step = step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


class PacedBackend(FakeBackend):
    """The fake Hermes stream (``chatkit.FakeBackend``), paced by the draft lane: after each delta it
    waits until the lane has sent a draft, so the drafts of a capture are always the same."""

    def __init__(self, events: list[Event], drafts: Callable[[], int], pump: Callable[[], Awaitable[Any]]) -> None:
        """``pump`` delivers the due outbox rows (production runs the outbox worker beside the turn; the
        lane yields while a row is due)."""
        super().__init__(events)
        self._drafts = drafts
        self._pump = pump

    async def stream(self, parts: list[dict[str, Any]], session_key: str, *, request_role: str | None = None,
                     instructions: str | None = None) -> AsyncIterator[Event]:  # fmt: skip
        self.calls.append((parts, session_key))
        self.instructions = instructions
        self.started.set()
        try:
            for event in self.events:
                before = self._drafts()
                yield event
                if event.kind != "delta":
                    continue
                for _ in range(10_000):
                    if self._drafts() > before:
                        break
                    await self._pump()
                    await asyncio.sleep(0)
                else:
                    raise AssertionError("the draft lane sent no draft")
        finally:
            self.closed.set()
