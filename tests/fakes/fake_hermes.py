"""Minimal fake Hermes answer stream (DESIGN §14.18.5 budget tests; the full relay is FEAT-007).

``FakeHermesStream.deltas()`` yields text deltas and then holds the stream open for ``hold_s`` (a
long answer still streaming) unless ``stop()`` is called, like ``stopped_message_generation``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence


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
