"""Injectable clock. Production code takes a ``Clock``; tests pass a ``FakeClock``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """Deterministic clock for tests; always timezone-aware UTC."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta | None = None, **kwargs: float) -> datetime:
        self._now = self._now + (delta if delta is not None else timedelta(**kwargs))
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value


def to_iso(dt: datetime) -> str:
    """UTC ISO-8601 with microseconds and a trailing Z (the DB text format; sorts lexically)."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def from_iso(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
