"""Size and filter preferences for autonomous discovery (DESIGN §9.14, §14.14.4; CR-004 Amendment B).

The only writer of ``discovery_prefs`` (asserted by ``tests/unit/gatekeeper/test_discovery_prefs.py``).
Preferences steer what the bot picks on its own; they never apply to user-directed (👤) work and
never change an approved proposal (which keeps its bound class). The hard ceiling
(``policy.diff``) is config-only: any ceiling key is refused here (U13).

Every change is audited, announced as an FYI and undoable for 60 s (``undo``). Precedence for a
repo: its DB row, then ``targets.yaml size_class``, then the global DB row (scope ``*``), then
``policy.yaml``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Annotated, Any

from pydantic import Field, ValidationError, field_validator

from ossbot.common.clock import Clock, from_iso, to_iso
from ossbot.common.config import (
    ConfigBundle,
    DiscoveryFilters,
    Language,
    OpenDiscoveryPolicy,
    Strict,
    Topic,
    resolve_target,
    unique_languages,
    unique_topics,
)
from ossbot.common.ids import new_id
from ossbot.common.schemas import PrefsUpdate
from ossbot.common.size import CLASS_BOUNDS, SelectableSizeClass
from ossbot.gatekeeper import db
from ossbot.gatekeeper.audit import AuditLog

UNDO_SECONDS = 60
FILTER_KEYS: frozenset[str] = frozenset(DiscoveryFilters.model_fields)
REPO_ONLY_KEYS: frozenset[str] = frozenset({"never"})  # Never this repo (§14.11.15)
CEILING_KEYS: frozenset[str] = frozenset({"diff", "max_files", "max_changed_lines", "max_test_lines", "ceiling"})
# Post-v1 open discovery (design §3.2): the Telegram-tunable part of policy.discovery.open, stored only
# in the "*" row's filters_json and kept out of DiscoveryFilters. Every count and budget is config-only.
OPEN_PREF_KEYS: frozenset[str] = frozenset({"open_enabled", "open_topics", "open_languages_preferred"})
CONFIG_ONLY_OPEN_KEYS: frozenset[str] = frozenset({
    "open_min_stars", "open_search_budget", "open_new_repos_per_cycle", "open_topics_per_cycle",
    "open_reserved_slots", "open_repo_cooldown_days", "open_pushed_within_days", "open_language_weight",
    "open_issue_labels",
})  # fmt: skip


class OpenPrefs(Strict):
    """The tunable view of open discovery; defaults come from ``policy.discovery.open``."""

    enabled: bool
    topics: Annotated[list[Topic], Field(min_length=1, max_length=30)]
    languages_preferred: Annotated[list[Language], Field(max_length=20)]

    @field_validator("topics")
    @classmethod
    def _topics_unique(cls, value: list[str]) -> list[str]:
        return unique_topics(value)

    @field_validator("languages_preferred")
    @classmethod
    def _languages_unique(cls, value: list[str]) -> list[str]:
        return unique_languages(value)


class PrefsRefused(ValueError):
    def __init__(self, status: int, code: str, detail: str = "") -> None:
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class EffectivePrefs:
    autonomous_max_class: SelectableSizeClass
    preferred_classes: tuple[SelectableSizeClass, ...]
    filters: DiscoveryFilters
    never_repo: bool = False
    sources: dict[str, str] = field(default_factory=dict)
    open: OpenPrefs | None = None  # always set by ``DiscoveryPrefs.effective``

    def as_dict(self) -> dict[str, Any]:
        return {
            "autonomous_max_class": self.autonomous_max_class,
            "preferred_classes": list(self.preferred_classes),
            "filters": self.filters.model_dump(),
            "never_repo": self.never_repo,
            "sources": self.sources,
            "open": self.open.model_dump() if self.open is not None else None,
        }


class DiscoveryPrefs:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        audit: AuditLog,
        config: Callable[[], ConfigBundle],
        notify: Callable[[str], None] = lambda text: None,
    ) -> None:
        self.conn = conn
        self.clock = clock
        self.audit = audit
        self.config = config
        self.notify = notify

    # ------------------------------------------------------------------- reads
    def _row(self, scope: str) -> dict[str, Any] | None:
        row = db.one(self.conn, "SELECT * FROM discovery_prefs WHERE scope = ?", (scope,))
        return dict(row) if row is not None else None

    def rows(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM discovery_prefs ORDER BY scope")]

    def effective(self, repo: str | None = None) -> EffectivePrefs:
        policy = self.config().policy
        max_class: SelectableSizeClass = policy.size.autonomous_max_class
        preferred = tuple(policy.size.preferred_classes)
        filters = policy.discovery_filters.model_dump()
        sources = {"autonomous_max_class": "policy.yaml", "preferred_classes": "policy.yaml", "open": "policy.yaml"}
        open_raw: dict[str, Any] = {}
        glob = self._row("*")
        if glob is not None:
            if glob["autonomous_max_class"]:
                max_class, sources["autonomous_max_class"] = glob["autonomous_max_class"], "prefs *"
            if glob["preferred_classes_json"]:
                preferred, sources["preferred_classes"] = tuple(json.loads(glob["preferred_classes_json"])), "prefs *"
            raw = json.loads(glob["filters_json"] or "{}")
            open_raw = {k: raw.pop(k) for k in list(raw) if k in OPEN_PREF_KEYS}  # never in DiscoveryFilters
            if open_raw:
                sources["open"] = "prefs *"
            filters.update(raw)
        open_prefs = _open_prefs(policy.discovery.open, open_raw)
        never = False
        if repo is not None:
            target = resolve_target(self.config().targets, repo)
            if target is not None and target.size_class:
                max_class, sources["autonomous_max_class"] = target.size_class, "targets.yaml"  # type: ignore[assignment]
            own = self._row(target.repo if target is not None else repo)
            if own is not None:
                if own["autonomous_max_class"]:
                    max_class, sources["autonomous_max_class"] = own["autonomous_max_class"], f"prefs {repo}"
                extra = json.loads(own["filters_json"] or "{}")
                never = bool(extra.pop("never", False))
                filters.update(extra)
        return EffectivePrefs(max_class, preferred, DiscoveryFilters.model_validate(filters), never, sources,
                              open_prefs)  # fmt: skip

    # ------------------------------------------------------------------ writes
    def _check(self, req: PrefsUpdate) -> tuple[str, dict[str, Any]]:
        scope = req.scope
        if scope != "*":
            target = resolve_target(self.config().targets, scope)
            if target is None:
                raise PrefsRefused(400, "not_a_target", scope)
            scope = target.repo
        elif req.autonomous_max_class == "inherit":
            raise PrefsRefused(400, "inherit_needs_repo")
        filters = dict(req.filters or {})
        bad = sorted(set(filters) & CEILING_KEYS)
        if bad:
            raise PrefsRefused(400, "ceiling_key", f"{bad} are config-only (policy.diff in config/policy.yaml)")
        config_only = sorted(set(filters) & CONFIG_ONLY_OPEN_KEYS)
        if config_only:  # design §3.2: Telegram can never raise an open-discovery count or budget
            raise PrefsRefused(400, "config_only", f"{config_only} are config-only (discovery.open in policy.yaml)")
        allowed = FILTER_KEYS | (REPO_ONLY_KEYS if scope != "*" else OPEN_PREF_KEYS)
        unknown = sorted(set(filters) - allowed - OPEN_PREF_KEYS)
        if unknown:
            raise PrefsRefused(400, "unknown_filter", ",".join(unknown))
        global_only = sorted(set(filters) & OPEN_PREF_KEYS - allowed)
        if global_only:
            raise PrefsRefused(400, "open_key_global_only", ",".join(global_only))
        if "never" in filters and not isinstance(filters["never"], bool):
            raise PrefsRefused(400, "bad_filter", "never")
        if req.autonomous_max_class is not None and req.autonomous_max_class != "inherit":
            lines, files = CLASS_BOUNDS[req.autonomous_max_class]
            ceiling = self.config().policy.diff
            if lines > ceiling.max_changed_lines or files > ceiling.max_files:
                raise PrefsRefused(400, "above_ceiling", str(req.autonomous_max_class))
        return scope, filters

    def update(self, req: PrefsUpdate, actor: str) -> dict[str, Any]:
        scope, filters = self._check(req)
        now = self.clock.now()
        with db.transaction(self.conn):
            before = self._row(scope)
            merged_filters = json.loads(before["filters_json"] or "{}") if before else {}
            merged_filters.update(filters)
            try:  # the merged filter set must still be a valid DiscoveryFilters
                DiscoveryFilters.model_validate(
                    {**self.config().policy.discovery_filters.model_dump(),
                     **{k: v for k, v in merged_filters.items() if k in FILTER_KEYS}}
                )  # fmt: skip
            except ValidationError as exc:
                raise PrefsRefused(400, "bad_filter", str(exc.errors()[0]["loc"])) from exc
            try:  # ... and the merged open part a valid OpenPrefs (design §3.2)
                _open_prefs(self.config().policy.discovery.open,
                            {k: v for k, v in merged_filters.items() if k in OPEN_PREF_KEYS})  # fmt: skip
            except ValidationError as exc:
                raise PrefsRefused(400, "bad_filter", "open_" + str(exc.errors()[0]["loc"][0])) from exc
            max_class = before["autonomous_max_class"] if before else None
            if req.autonomous_max_class is not None:
                max_class = None if req.autonomous_max_class == "inherit" else req.autonomous_max_class
            preferred = before["preferred_classes_json"] if before else None
            if req.preferred_classes is not None:
                preferred = json.dumps(req.preferred_classes)
            after = {"scope": scope, "autonomous_max_class": max_class, "preferred_classes_json": preferred,
                     "filters_json": json.dumps(merged_filters, sort_keys=True) if merged_filters else None,
                     "updated_by": actor, "updated_at": to_iso(now)}  # fmt: skip
            self.conn.execute(
                "INSERT INTO discovery_prefs (scope, autonomous_max_class, preferred_classes_json, filters_json,"
                " updated_by, updated_at) VALUES (:scope, :autonomous_max_class, :preferred_classes_json,"
                " :filters_json, :updated_by, :updated_at) ON CONFLICT(scope) DO UPDATE SET"
                " autonomous_max_class = excluded.autonomous_max_class,"
                " preferred_classes_json = excluded.preferred_classes_json, filters_json = excluded.filters_json,"
                " updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                after,
            )
            undo_id = new_id("u")
            expires = now + timedelta(seconds=UNDO_SECONDS)
            self.conn.execute(
                "INSERT INTO discovery_prefs_undo (id, scope, previous_json, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (undo_id, scope, json.dumps(before) if before else None, to_iso(now), to_iso(expires)),
            )
            self.audit.append(actor, "discovery_prefs_changed", {"scope": scope, "before": before, "after": after})
            self.notify(f"⚙️ Discovery preferences changed ({scope}): {_describe(after)}. Undo within 60 s.")
        return {"undo_id": undo_id, "undo_expires_at": to_iso(expires), "effective": self.effective(
            None if scope == "*" else scope).as_dict()}  # fmt: skip

    def undo(self, undo_id: str, actor: str) -> dict[str, Any]:
        now = self.clock.now()
        with db.transaction(self.conn):
            row = db.one(self.conn, "SELECT * FROM discovery_prefs_undo WHERE id = ?", (undo_id,))
            if row is None:
                raise PrefsRefused(404, "unknown_undo")
            if row["used_at"] is not None or from_iso(row["expires_at"]) <= now:
                raise PrefsRefused(409, "too_late")
            newer = db.one(
                self.conn, "SELECT 1 FROM discovery_prefs_undo WHERE scope = ? AND created_at > ? LIMIT 1",
                (row["scope"], row["created_at"]),
            )  # fmt: skip
            if newer is not None:
                raise PrefsRefused(409, "superseded")
            previous = json.loads(row["previous_json"]) if row["previous_json"] else None
            if previous is None:
                self.conn.execute("DELETE FROM discovery_prefs WHERE scope = ?", (row["scope"],))
            else:
                self.conn.execute(
                    "UPDATE discovery_prefs SET autonomous_max_class = :autonomous_max_class,"
                    " preferred_classes_json = :preferred_classes_json, filters_json = :filters_json,"
                    " updated_by = :updated_by, updated_at = :updated_at WHERE scope = :scope",
                    previous,
                )
            self.conn.execute("UPDATE discovery_prefs_undo SET used_at = ? WHERE id = ?", (to_iso(now), undo_id))
            self.audit.append(actor, "discovery_prefs_undone", {"scope": row["scope"], "undo_id": undo_id})
            self.notify(f"↩️ Discovery preferences restored ({row['scope']}).")
        return {"scope": row["scope"], "restored": previous is not None}

    # ------------------------------------------------- gate-1 "Never" records (§14.11.15 step 5)
    def never_repo(self, repo: str, actor: str) -> dict[str, Any]:
        return self.update(PrefsUpdate(scope=repo, filters={"never": True}), actor)

    def never_kind(self, kind: str, actor: str) -> dict[str, Any]:
        current = self.effective().filters
        excluded = list(dict.fromkeys([*current.excluded_kinds, kind]))
        allowed = [k for k in current.allowed_kinds if k != kind]
        return self.update(PrefsUpdate(filters={"excluded_kinds": excluded, "allowed_kinds": allowed}), actor)


def _open_prefs(policy: OpenDiscoveryPolicy, open_raw: dict[str, Any]) -> OpenPrefs:
    """``policy.discovery.open`` overlaid with the stored ``open_*`` keys (raises ``ValidationError``)."""
    base: dict[str, Any] = {"enabled": policy.enabled, "topics": list(policy.topics),
                            "languages_preferred": list(policy.languages_preferred)}  # fmt: skip
    return OpenPrefs.model_validate({**base, **{k.removeprefix("open_"): v for k, v in open_raw.items()}})


def _describe(after: dict[str, Any]) -> str:
    parts = []
    if after["autonomous_max_class"]:
        parts.append(f"auto size ≤ {after['autonomous_max_class']}")
    if after["preferred_classes_json"]:
        parts.append(f"prefer {', '.join(json.loads(after['preferred_classes_json']))}")
    if after["filters_json"]:
        stored = json.loads(after["filters_json"])
        names = sorted(k for k in stored if k not in OPEN_PREF_KEYS)
        if names:
            parts.append("filters " + ", ".join(names))
        if "open_enabled" in stored:
            parts.append(f"open discovery {'on' if stored['open_enabled'] else 'off'}")
        if "open_topics" in stored:
            parts.append(f"topics {len(stored['open_topics'])}")
        if "open_languages_preferred" in stored:
            parts.append(f"languages {len(stored['open_languages_preferred'])}")
    return "; ".join(parts) or "back to defaults"
