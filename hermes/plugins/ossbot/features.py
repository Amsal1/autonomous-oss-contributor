"""Per-feature SQLite stores for the assistant's own features (DESIGN §14.12.7, §14.18.3; PLAN item 37).

Each registered feature gets one database, ``$HERMES_HOME/ossbot-features/<feature>.db`` (0700 dir,
0600 file, uid hermes), with one table of JSON records. It holds the owner's personal data a feature
collects through Hermes (e.g. ``food_log``: the meals and protein estimates of photo turns), never
anything ossbot's OSS pipeline relies on: the gatekeeper does not read it, and a new feature is a new
``FEATURES`` entry (name, purpose, record bounds) and nothing else. Stdlib only; never imports ossbot.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, data TEXT NOT NULL, created_at REAL NOT NULL,"
    " updated_at REAL NOT NULL)"
)


@dataclass(frozen=True)
class Feature:
    name: str
    purpose: str
    max_record_bytes: int = 4096
    max_records: int = 20_000


FEATURES: dict[str, Feature] = {
    "food_log": Feature(
        "food_log",
        "Meals the owner logged (often from a food photo) with estimated protein, calories and notes;"
        " for intake summaries.",
    ),
}


def root() -> Path:
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / "ossbot-features"


class FeatureStore:
    def __init__(self, feature: str, base: Path | None = None) -> None:
        if feature not in FEATURES:
            raise ValueError(f"unknown feature {feature[:40]!r}; known: {', '.join(sorted(FEATURES))}")
        self.feature = FEATURES[feature]
        self.base = base or root()

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """One transaction on the feature's database, closed afterwards."""
        self.base.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.base / f"{self.feature.name}.db"
        if path.is_symlink():
            raise ValueError("feature store is a symlink")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        os.close(fd)
        conn = sqlite3.connect(path, timeout=5.0)
        try:
            with conn:
                conn.execute(SCHEMA)
                yield conn
        finally:
            conn.close()

    def save(self, key: str, data: dict[str, Any]) -> dict[str, Any]:
        if not KEY_RE.match(key or ""):
            raise ValueError("key must be 1-80 characters of letters, digits and _.:-")
        if not isinstance(data, dict):
            raise ValueError("data must be a JSON object")
        text = json.dumps(data, ensure_ascii=False, sort_keys=True)
        if len(text.encode("utf-8")) > self.feature.max_record_bytes:
            raise ValueError(f"record larger than {self.feature.max_record_bytes} bytes")
        now = time.time()
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            exists = conn.execute("SELECT 1 FROM records WHERE key = ?", (key,)).fetchone() is not None
            if not exists and count >= self.feature.max_records:
                raise ValueError(f"{self.feature.name} is full ({self.feature.max_records} records)")
            conn.execute(
                "INSERT INTO records (key, data, created_at, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(key)"
                " DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
                (key, text, now, now),
            )
        return {"feature": self.feature.name, "key": key, "updated": exists}

    def query(self, prefix: str = "", limit: int = 50) -> list[dict[str, Any]]:
        if prefix and not KEY_RE.match(prefix):
            raise ValueError("prefix must look like a key")
        limit = max(1, min(int(limit), 200))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, data, created_at FROM records WHERE key >= ? AND key < ? ORDER BY key DESC LIMIT ?",
                (prefix, prefix + "\U0010ffff", limit),
            ).fetchall()
        return [{"key": k, "data": json.loads(d), "created_at": round(c)} for k, d, c in rows]

    def delete(self, key: str) -> bool:
        if not KEY_RE.match(key or ""):
            raise ValueError("bad key")
        with self._connect() as conn:
            return conn.execute("DELETE FROM records WHERE key = ?", (key,)).rowcount > 0
