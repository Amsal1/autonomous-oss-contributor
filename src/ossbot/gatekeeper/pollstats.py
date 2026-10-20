"""Telegram long-poll health (CR-001 item 1): last successful ``getUpdates``, failures, reconnects.

The poller itself is python-telegram-bot's (outbound HTTPS only, retried with backoff on network
errors, pending updates kept server-side because the offset only advances on success). This
module only records what happened so ``/status``, ``ossctl status`` and ``status.ps1`` can show
it, and logs every recovery after an outage (IP change, Wi-Fi switch, sleep/hibernate).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from ossbot.common.clock import Clock, SystemClock, to_iso

log = logging.getLogger(__name__)


@dataclass
class PollStats:
    clock: Clock = field(default_factory=SystemClock)
    last_ok_at: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    total_failures: int = 0
    reconnects: int = 0
    first_batch: int | None = None  # updates in the first successful getUpdates (the "Back online" count)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def ok(self, updates: int = 0) -> None:
        with self._lock:
            if self.first_batch is None:
                self.first_batch = updates
            if self.consecutive_failures:
                self.reconnects += 1
                log.warning(
                    "telegram polling reconnected after %d failed attempt(s)", self.consecutive_failures,
                    extra={"event": "telegram_reconnected", "failures": self.consecutive_failures},
                )  # fmt: skip
            self.consecutive_failures = 0
            self.last_ok_at = to_iso(self.clock.now())

    def failed(self, error: str) -> None:
        with self._lock:
            self.consecutive_failures += 1
            self.total_failures += 1
            self.last_error = error[:200]
            if self.consecutive_failures == 1:
                log.warning("telegram polling failed: %s (retrying with backoff)", self.last_error,
                            extra={"event": "telegram_poll_failed"})  # fmt: skip

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "last_ok_at": self.last_ok_at,
                "consecutive_failures": self.consecutive_failures,
                "total_failures": self.total_failures,
                "reconnects": self.reconnects,
                "last_error": self.last_error,
            }
