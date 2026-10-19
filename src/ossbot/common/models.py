"""Model resolution for every LLM role (DESIGN §16.1, ADR-0009, N6).

``config/models.yaml`` is the one global switch. ``resolve(role)`` follows the indirections
(``default`` and role names, e.g. ``scout: {model: planner}``), then applies the catalog-or-forced
rule: an id that is neither in the latest Kiro catalog nor force-set falls back to the first
``fallbacks`` entry that is; with none, the role is unresolvable (``model_unavailable``) and
nothing may be sent to sandboxd. Effort comes from the role, else the inherited role, else
``default.effort``.

``ModelsFile`` reads the file fresh on every call (mtime/size cached) and keeps the last good
version when an edit breaks it (``on_broken`` alerts once per broken version); the last good
copy can be persisted so a restart with a broken file still resolves.

``parse_list_models`` turns the output of ``kiro-cli chat --list-models --format json`` into
catalog entries (shape: §23 item 9, ADR-0009 addendum).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from ossbot.common.config import MODEL_ID_RE, ROLE_NAMES, ConfigError, Effort, ModelsConfig, RoleModel, parse_models
from ossbot.common.schemas import CatalogEntry

Source = Literal["role", "default", "inherited", "fallback"]
CATALOG_MAX = 200  # POST /v1/models/catalog accepts at most this many entries (R4-N3)


# Every live check that calls a model (kiro smoke, doctor, the safety probes, live e2e/system tests)
# resolves this role; its id lives only in config/models.yaml (owner decision 2026-10-18).
LIVE_TEST_ROLE = "test"


class ModelUnavailable(RuntimeError):
    """No model of the role (nor any fallback) is in the catalog or force-set."""

    error_class = "model_unavailable"


class DistinctUnavailable(ModelUnavailable):
    """``require_distinct_from`` cannot be met (the reviewer would run on the coder's model): the
    runner pauses the pipeline and alerts instead of running a non-independent review."""

    error_class = "model_collision"


@dataclass(frozen=True)
class Catalog:
    """The ids sandboxd accepts: the latest refresh plus force-set ids (N6)."""

    ids: frozenset[str]
    forced: frozenset[str] = frozenset()

    @classmethod
    def from_rows(cls, rows: Iterable[dict[str, Any]]) -> Catalog:
        """From ``GET /v1/models/catalog`` (``[{model_id, forced}]``)."""
        ids: set[str] = set()
        forced: set[str] = set()
        for row in rows:
            (forced if row.get("forced") else ids).add(str(row["model_id"]))
        return cls(frozenset(ids), frozenset(forced))

    def accepts(self, model: str) -> bool:
        return model in self.ids or model in self.forced


@dataclass(frozen=True)
class ResolvedModel:
    role: str
    model: str
    effort: Effort
    source: Source
    requested: str  # the id the indirections led to, before any fallback


def _chain(cfg: ModelsConfig, role: str) -> tuple[str, Effort, Source]:
    """Follow role -> role -> default; the validator already refused cycles."""
    entry: RoleModel = getattr(cfg.roles, role)
    effort: Effort | None = entry.effort
    source: Source = "role"
    seen = {role}
    while entry.model in ROLE_NAMES:
        if entry.model in seen:  # defence in depth: parse_models rejects cycles
            raise ModelUnavailable(f"role reference cycle at {role}")
        seen.add(entry.model)
        entry = getattr(cfg.roles, entry.model)
        effort = effort or entry.effort
        source = "inherited"
    if entry.model == "default":
        return cfg.default.model, effort or cfg.default.effort, "default" if source == "role" else "inherited"
    return entry.model, effort or cfg.default.effort, source


def requested(cfg: ModelsConfig, role: str) -> tuple[str, Effort]:
    """The model id and effort the role's indirections lead to, before the catalog and any fallback
    (for clients that check against the catalog a Kiro session advertises, e.g. the Hermes probe)."""
    if role not in ROLE_NAMES:
        raise ModelUnavailable(f"unknown role {role}")
    model, effort, _source = _chain(cfg, role)
    return model, effort


def role_fallbacks(cfg: ModelsConfig, role: str) -> list[str]:
    """``roles.<r>.fallback`` along the role-name chain: "none" -> [], a list -> it, unset -> the
    global ``fallbacks`` (e.g. ``test_vision: {model: test}`` inherits test's "none")."""
    entry: RoleModel = getattr(cfg.roles, role)
    seen = {role}
    while True:
        if entry.fallback == "none":
            return []
        if isinstance(entry.fallback, list):
            return list(entry.fallback)
        if entry.model not in ROLE_NAMES or entry.model in seen:
            return list(cfg.fallbacks)
        seen.add(entry.model)
        entry = getattr(cfg.roles, entry.model)


def resolve_config(cfg: ModelsConfig, role: str, catalog: Catalog) -> ResolvedModel:
    """Resolve one role against the catalog. ``require_distinct_from`` (reviewer vs coder) moves
    to the first accepted fallback that differs from the other role's model, when there is one."""
    if role not in ROLE_NAMES:
        raise ModelUnavailable(f"unknown role {role}")
    requested, effort, source = _chain(cfg, role)
    avoid: str | None = None
    distinct = getattr(cfg.roles, role).require_distinct_from
    if distinct is not None:
        with contextlib.suppress(ModelUnavailable):
            avoid = resolve_config(cfg, distinct, catalog).model
    if catalog.accepts(requested) and requested != avoid:
        return ResolvedModel(role, requested, effort, source, requested)
    fallbacks = role_fallbacks(cfg, role)
    if not fallbacks and not catalog.accepts(requested):
        raise ModelUnavailable(f"{role}: {requested} is not in the Kiro catalog and roles.{role} allows no fallback"
                               f" (set it with: ossctl model set {role} <catalog id>)")  # fmt: skip
    for fallback in fallbacks:
        # a fallback equal to the role's own (missing or colliding) model is no fallback at all
        if fallback != requested and catalog.accepts(fallback) and fallback != avoid:
            return ResolvedModel(role, fallback, effort, "fallback", requested)
    if avoid is not None and (catalog.accepts(requested) or catalog.accepts(avoid)):
        # require_distinct_from stays enforced after fallback: never run the reviewer on the coder's model
        raise DistinctUnavailable(f"{role}: no catalog model distinct from {distinct} ({avoid}) is available")
    raise ModelUnavailable(f"{role}: {requested} is not in the Kiro catalog and no fallback is")


class ModelsFile:
    """mtime-cached reader of ``config/models.yaml`` with a last-good copy (§16.1)."""

    def __init__(
        self,
        path: Path,
        *,
        last_good_path: Path | None = None,
        on_broken: Callable[[str], None] | None = None,
    ) -> None:
        self.path = path
        self.last_good_path = last_good_path
        self.on_broken = on_broken
        self._stamp: tuple[int, int] | None = None
        self._good: ModelsConfig | None = None
        self._broken_stamp: tuple[int, int] | None = None

    def _load_last_good(self) -> ModelsConfig | None:
        if self.last_good_path is None:
            return None
        try:
            return parse_models(self.last_good_path.read_bytes())
        except (OSError, ConfigError):
            return None

    def _save_last_good(self, data: bytes) -> None:
        if self.last_good_path is None:
            return
        tmp = self.last_good_path.with_name(f".{self.last_good_path.name}.tmp-{os.getpid()}")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, self.last_good_path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()

    def get(self) -> ModelsConfig:
        try:
            st = os.stat(self.path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError as exc:
            return self._fallback(f"models.yaml unreadable: {exc.strerror}", None)
        if stamp == self._stamp and self._good is not None:
            return self._good
        if stamp == self._broken_stamp:
            return self._fallback(None, stamp)
        try:
            data = self.path.read_bytes()
            cfg = parse_models(data)
        except OSError as exc:
            return self._fallback(f"models.yaml unreadable: {exc.strerror}", stamp)
        except ConfigError as exc:
            return self._fallback(f"models.yaml invalid, last good version kept: {exc}", stamp)
        self._stamp, self._good, self._broken_stamp = stamp, cfg, None
        self._save_last_good(data)
        return cfg

    def _fallback(self, reason: str | None, stamp: tuple[int, int] | None) -> ModelsConfig:
        if reason is not None and stamp != self._broken_stamp:
            self._broken_stamp = stamp
            if self.on_broken is not None:
                self.on_broken(reason)
        good = self._good or self._load_last_good()
        if good is None:
            raise ModelUnavailable(reason or "models.yaml invalid and no last good version")
        self._good = good
        return good

    def resolve(self, role: str, catalog: Catalog) -> ResolvedModel:
        return resolve_config(self.get(), role, catalog)


# ----------------------------------------------------------------- list-models output
_ID_KEYS = ("model_id", "modelId", "id")
_NAME_KEYS = ("model_name", "modelName", "name", "display_name")
_INPUT_KEYS = ("supported_input_types", "supportedInputTypes", "input_types")


def _first(entry: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in entry:
            return entry[key]
    return None


def parse_list_models(raw: bytes) -> list[CatalogEntry]:
    """Parse ``kiro-cli chat --list-models --format json``.

    Accepted shapes: ``{"models": [...], ...}`` (kiro-cli 2.32 ``ModelListResult``), a bare list,
    or ACP-style ``{"availableModels": [...]}``. Entries carry ``model_id`` (or ``modelId``/``id``),
    an optional name and ``supported_input_types``. Ids outside the --force regex, duplicates and
    entries beyond 200 are dropped; this is untrusted tool output, nothing else is kept.
    """
    try:
        data: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"list-models output is not JSON: {exc}") from exc
    if isinstance(data, dict):
        items = data.get("models", data.get("availableModels"))
    else:
        items = data
    if not isinstance(items, list):
        raise ValueError("list-models output has no model list")
    out: list[CatalogEntry] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = _first(item, _ID_KEYS)
        if not isinstance(model_id, str) or not re.fullmatch(MODEL_ID_RE, model_id) or model_id in seen:
            continue
        name = _first(item, _NAME_KEYS)
        inputs = _first(item, _INPUT_KEYS)
        try:
            entry = CatalogEntry(
                model_id=model_id,
                name=name[:200] if isinstance(name, str) else None,
                input_types=[str(t) for t in inputs][:16] if isinstance(inputs, list) else None,
            )
        except ValidationError:
            entry = CatalogEntry(model_id=model_id, name=name[:200] if isinstance(name, str) else None)
        seen.add(model_id)
        out.append(entry)
        if len(out) == CATALOG_MAX:
            break
    return out
