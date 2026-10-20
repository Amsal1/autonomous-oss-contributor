"""Feature cards: the pattern and the registry (DESIGN §14.12.7, §14.18.3; v1 ships no feature).

A feature is a package ``ossbot.gatekeeper.features.<name>`` exposing ``FEATURE = FeatureSpec(...)``,
registered under the entry point group ``ossbot.telegram_features`` and listed in
``telegram.yaml features.enabled``. Hermes calls ``ossbot_feature_card(feature, kind, data)``; the
gatekeeper validates ``data`` with the feature's pydantic model (``extra="forbid"``, bounded fields),
stores it in the feature's own SQLite store, renders the card with ``telegram.ui.kit`` only and sends
it where U12 allows. Features have ``read`` and ``personal`` actions only: they never import the
approval, submit, writer, state-machine or control modules (import-linter contract).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ossbot.gatekeeper.telegram.ui.kit import View

log = logging.getLogger(__name__)
ENTRY_POINT_GROUP = "ossbot.telegram_features"
Card = Callable[[BaseModel, Mapping[str, Any]], View]  # (validated data, ux prefs) -> the card view


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    version: int
    schema_models: Mapping[str, type[BaseModel]]  # card kind -> the data model Hermes must match
    cards: Mapping[str, Card]  # card kind -> renderer
    store_migrations: tuple[str, ...] = ()  # SQL applied in order to the feature's own store
    actions: tuple[Any, ...] = field(default_factory=tuple)


REGISTRY: dict[str, FeatureSpec] = {}


def register(spec: FeatureSpec) -> None:
    if set(spec.schema_models) != set(spec.cards):
        raise ValueError(f"feature {spec.name}: every card kind needs a model and a renderer")
    REGISTRY[spec.name] = spec


def load(enabled: list[str]) -> None:
    """Register the enabled features from their entry points (a missing or broken one is logged)."""
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        if ep.name not in enabled or ep.name in REGISTRY:
            continue
        try:
            spec = ep.load()
            register(spec if isinstance(spec, FeatureSpec) else spec.FEATURE)
        except Exception:
            log.exception("feature %s failed to load", ep.name, extra={"event": "feature_load_failed"})


def get(name: str, enabled: list[str]) -> FeatureSpec | None:
    return REGISTRY.get(name) if name in enabled else None


class FeatureStore:
    """``/srv/ossbot/gatekeeper/features/<name>.db``: the feature's only database."""

    def __init__(self, root: Path, spec: FeatureSpec) -> None:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.conn = sqlite3.connect(root / f"{spec.name}.db")
        self.conn.execute("CREATE TABLE IF NOT EXISTS cards (id INTEGER PRIMARY KEY, kind TEXT NOT NULL,"
                          " data_json TEXT NOT NULL, turn_id INTEGER, created_at TEXT NOT NULL)")  # fmt: skip
        for sql in spec.store_migrations:
            self.conn.executescript(sql)
        self.conn.commit()

    def add(self, kind: str, data: BaseModel, turn_id: int, created_at: str) -> int:
        cur = self.conn.execute("INSERT INTO cards (kind, data_json, turn_id, created_at) VALUES (?, ?, ?, ?)",
                                (kind, json.dumps(data.model_dump(mode="json")), turn_id, created_at))  # fmt: skip
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def close(self) -> None:
        self.conn.close()
