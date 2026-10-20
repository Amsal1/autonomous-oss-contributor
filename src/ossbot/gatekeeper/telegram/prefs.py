"""Runtime UX preferences in ``ux_prefs`` (DESIGN §14.20.2): rich views, quiet mode, notification
toggles, the rich-fallback window. Presentation only: nothing here can approve, publish or change a
limit (the ceiling and the size preferences live elsewhere, U13)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from ossbot.common.clock import Clock, from_iso, to_iso
from ossbot.gatekeeper import db

# key -> allowed values (None = free JSON written only by gatekeeper code, never from a token arg)
KEYS: dict[str, tuple[str, ...] | None] = {
    "rich_views": ("auto", "on", "off"),
    "file_view": ("auto", "classic"),
    "rich_unavailable_until": None,
    "quiet_until": None,
    "notify": None,  # {category: bool} for telegram.notify.CATEGORIES (§14.13.2)
    "last_seen": None,  # the frontend's heartbeat (ISO), the "Back online · offline N" span (§14.16)
    "away": None,  # {"cards": n, "messages": n} rung silently during quiet hours (the 🌅 summary)
    "short_description": None,
    "flat_mode_reason": None,
}


class UxPrefs:
    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        self.conn, self.clock = conn, clock

    def get(self, key: str, default: Any = None) -> Any:
        row = db.one(self.conn, "SELECT value_json FROM ux_prefs WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except ValueError:
            return default

    def set(self, key: str, value: Any) -> None:
        if key not in KEYS:
            raise KeyError(key)
        allowed = KEYS[key]
        if allowed is not None and value not in allowed:
            raise ValueError(f"{key}: {value!r} not in {allowed}")
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO ux_prefs (key, value_json, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE"
                " SET value_json = excluded.value_json, updated_at = excluded.updated_at",
                (key, json.dumps(value, sort_keys=True), to_iso(self.clock.now())),
            )

    def until(self, key: str) -> datetime | None:
        """A stored ISO time that is still in the future, else None."""
        raw = self.get(key)
        if not isinstance(raw, str):
            return None
        try:
            when = from_iso(raw)
        except ValueError:
            return None
        return when if when > self.clock.now() else None
