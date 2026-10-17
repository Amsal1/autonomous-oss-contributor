"""Model validation against the gatekeeper catalog (DESIGN §10.2, N6).

``model`` must be a ``model_id`` in ``GET /v1/models/catalog`` or a row force-set by
``ossctl model set --force`` (``forced = 1``; also listed by the catalog endpoint). The catalog
is cached for 5 minutes; a failed refresh keeps refusing unknown ids (fail closed) and keeps the
last good list only until it expires.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ossbot.common.clock import Clock

CACHE_TTL = timedelta(minutes=5)
CatalogFetch = Callable[[], list[dict[str, Any]]]


class UnknownModel(ValueError):
    pass


class CatalogCache:
    def __init__(self, fetch: CatalogFetch, clock: Clock, ttl: timedelta = CACHE_TTL) -> None:
        self._fetch = fetch
        self._clock = clock
        self._ttl = ttl
        self._ids: frozenset[str] = frozenset()
        self._loaded: datetime | None = None
        self._lock = threading.Lock()

    def ids(self) -> frozenset[str]:
        with self._lock:
            now = self._clock.now()
            if self._loaded is None or now - self._loaded >= self._ttl:
                try:
                    rows = self._fetch()
                except Exception:  # noqa: BLE001 - any refresh failure: expire the cache, fail closed
                    self._ids, self._loaded = frozenset(), None
                    return self._ids
                self._ids = frozenset(str(r["model_id"]) for r in rows if isinstance(r, dict) and "model_id" in r)
                self._loaded = now
            return self._ids

    def validate(self, model: str) -> None:
        if model not in self.ids():
            raise UnknownModel(f"model {model!r} is not in the catalog")
