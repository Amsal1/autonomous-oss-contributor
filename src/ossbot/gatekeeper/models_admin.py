"""Model switch writer and catalog (DESIGN §16, N2, N6, R4-N3).

The gatekeeper is the only writer of ``config/models.yaml``: ruamel round-trip keeps comments,
the new content is validated before an atomic ``os.replace`` of a same-directory temp file.
New catalog ids are announced once each (``model_catalog.notified``); the first fill of an empty
catalog sends one summary instead (§16.3; the button card is item 47). Every switch that changes
the file (any role, test roles included, forced or not) is audited and announced by one owner FYI
in the same transaction.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ossbot.common.clock import Clock, to_iso
from ossbot.common.config import MODEL_ID_RE, ROLE_NAMES, ConfigError, ModelsConfig, parse_models
from ossbot.common.schemas import CatalogEntry
from ossbot.common.yamlio import dump_roundtrip, load_roundtrip
from ossbot.gatekeeper import db
from ossbot.gatekeeper.audit import AuditLog


class ModelSetError(ValueError):
    pass


class ModelsAdmin:
    def __init__(
        self,
        path: Path,
        conn: sqlite3.Connection,
        clock: Clock,
        audit: AuditLog,
        notify: Callable[[str], None] = lambda text: None,
    ) -> None:
        self.path = path
        self.conn = conn
        self.clock = clock
        self.audit = audit
        self.notify = notify
        # (model_id, name) -> True when it sent the Telegram new-model card (§16.3, set by the frontend)
        self.on_new_model: Callable[[str, str | None], bool] | None = None

    def read(self) -> ModelsConfig:
        return parse_models(self.path.read_bytes())

    # ----------------------------------------------------------------- catalog
    def catalog(self) -> list[dict[str, Any]]:
        """The latest catalog (rows seen in the newest refresh) plus forced ids."""
        newest = self.conn.execute("SELECT MAX(last_seen) FROM model_catalog WHERE forced = 0").fetchone()[0]
        rows = self.conn.execute(
            "SELECT model_id, display_name, forced, raw_json FROM model_catalog WHERE forced = 1 OR last_seen = ?"
            " ORDER BY model_id",
            (newest,),
        ).fetchall()
        return [
            {
                "model_id": r["model_id"],
                "name": r["display_name"],
                "forced": bool(r["forced"]),
                "input_types": _input_types(r["raw_json"]),
            }
            for r in rows
        ]

    def in_catalog(self, model_id: str) -> bool:
        return any(entry["model_id"] == model_id for entry in self.catalog())

    def _notify_enabled(self) -> bool:
        try:
            return self.read().catalog.notify_new_models
        except (OSError, ConfigError):
            return True

    def upsert_catalog(self, models: list[CatalogEntry], actor: str) -> list[str]:
        now = to_iso(self.clock.now())
        new_ids: list[str] = []
        notify = self._notify_enabled()
        with db.transaction(self.conn):
            first_fill = db.one(self.conn, "SELECT 1 FROM model_catalog WHERE forced = 0 LIMIT 1") is None
            for entry in models:
                existing = db.one(self.conn, "SELECT model_id FROM model_catalog WHERE model_id = ?", (entry.model_id,))
                if existing is None:
                    new_ids.append(entry.model_id)
                    self.conn.execute(
                        "INSERT INTO model_catalog (model_id, display_name, raw_json, first_seen, last_seen)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (entry.model_id, entry.name, json.dumps(entry.model_dump()), now, now),
                    )
                else:
                    self.conn.execute(
                        "UPDATE model_catalog SET display_name = ?, raw_json = ?, last_seen = ? WHERE model_id = ?",
                        (entry.name, json.dumps(entry.model_dump()), now, entry.model_id),
                    )
            self.audit.append(actor, "model_catalog_upsert", {"count": len(models), "new_ids": new_ids})
            ids = [m.model_id for m in models]
            marks = ",".join("?" * len(ids))
            pending = self.conn.execute(
                f"SELECT model_id, display_name FROM model_catalog WHERE notified = 0 AND forced = 0"  # noqa: S608
                f" AND model_id IN ({marks}) ORDER BY model_id",
                ids,
            ).fetchall()
            if notify and first_fill:
                self.notify(f"Kiro catalog loaded: {len(models)} models")
            elif notify:
                for row in pending:
                    name = row["display_name"]
                    if self.on_new_model is not None and self.on_new_model(row["model_id"], name):
                        continue
                    suffix = f" ({name})" if name and name != row["model_id"] else ""
                    self.notify(f"New Kiro model: {row['model_id']}{suffix}")
            self.conn.execute(f"UPDATE model_catalog SET notified = 1 WHERE model_id IN ({marks})", ids)  # noqa: S608
        return new_ids

    def request_refresh(self, actor: str) -> str:
        now = to_iso(self.clock.now())
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO runner_requests (kind, requested_at, actor) VALUES ('catalog_refresh', ?, ?)"
                " ON CONFLICT(kind) DO UPDATE SET requested_at = excluded.requested_at, actor = excluded.actor",
                (now, actor),
            )
            self.audit.append(actor, "model_catalog_refresh_requested", {})
        return now

    def claim_refresh(self, actor: str) -> bool:
        with db.transaction(self.conn):
            cur = self.conn.execute("DELETE FROM runner_requests WHERE kind = 'catalog_refresh'")
            claimed = cur.rowcount > 0
            if claimed:
                self.audit.append(actor, "model_catalog_refresh_claimed", {})
        return claimed

    # ------------------------------------------------------------------ writer
    def set_model(self, role: str, model: str, effort: str | None, force: bool, actor: str) -> str:
        if role != "default" and role not in ROLE_NAMES:
            raise ModelSetError(f"unknown role {role}")
        if not re.fullmatch(MODEL_ID_RE, model):
            raise ModelSetError("model id does not match the allowed pattern")
        forced = False  # a forced, non-catalog id (references need no catalog entry)
        if model == "default" or model in ROLE_NAMES:
            if role == "default":
                raise ModelSetError("the default model must be a model id, not a reference")
            # 'default' and role names are references (inherit); cycles are refused by parse_models
        elif force:
            forced = True
            with db.transaction(self.conn):
                now = to_iso(self.clock.now())
                self.conn.execute(
                    "INSERT INTO model_catalog (model_id, first_seen, last_seen, forced, forced_by, forced_at)"
                    " VALUES (?, ?, ?, 1, ?, ?) ON CONFLICT(model_id) DO UPDATE SET forced = 1,"
                    " forced_by = excluded.forced_by, forced_at = excluded.forced_at",
                    (model, now, now, actor, now),
                )
                self.audit.append(actor, "model_forced", {"model_id": model})
        else:
            entry = next((e for e in self.catalog() if e["model_id"] == model), None)
            if entry is None:
                raise ModelSetError(f"{model} is not in the current Kiro model catalog")
            types = entry["input_types"]
            if role == "vision" and types is not None and "IMAGE" not in {t.upper() for t in types}:
                raise ModelSetError(f"{model} does not accept images (vision needs an image-capable model)")
        old_text = self.path.read_text(encoding="utf-8")
        data = load_roundtrip(old_text)
        node = data["default"] if role == "default" else data["roles"][role]
        old_model, old_effort = node.get("model"), node.get("effort")
        node["model"] = model
        if effort is not None:
            node["effort"] = effort
        new_text = dump_roundtrip(data)
        try:
            parse_models(new_text.encode("utf-8"))
        except ConfigError as exc:
            raise ModelSetError(str(exc)) from exc
        tmp = self.path.with_name(f".models.yaml.tmp-{os.getpid()}")
        tmp.write_text(new_text, encoding="utf-8")
        # Keep the original mode and group (drvfs metadata: e.g. ossadmin:osscfg 0775), so the
        # live readers (runner, Hermes, the operator) can still read it after the replace.
        current = os.stat(self.path)
        os.chmod(tmp, stat.S_IMODE(current.st_mode))
        with contextlib.suppress(PermissionError):  # not in the group: the mode alone keeps it readable
            os.chown(tmp, -1, current.st_gid)
        os.replace(tmp, self.path)
        diff = "".join(
            difflib.unified_diff(old_text.splitlines(True), new_text.splitlines(True), "a/models.yaml", "b/models.yaml")
        )
        with db.transaction(self.conn):  # audited and announced together (§16: audited, FYI, catalog-validated)
            self.audit.append(actor, "model_set", {"role": role, "model": model, "effort": effort, "force": force})
            if diff:
                self.notify(_switch_fyi(role, (old_model, old_effort), (model, effort or old_effort), actor, forced))
        return diff


def _switch_fyi(role: str, old: tuple[Any, Any], new: tuple[Any, Any], actor: str, force: bool) -> str:
    def show(model: Any, effort: Any) -> str:
        return f"{model} ({effort})" if effort else str(model)

    forced = " · forced, not in the catalog" if force else ""
    return f"🧠 Model: {role} {show(*old)} → {show(*new)} (by {actor}){forced}"


def _input_types(raw: str | None) -> list[str] | None:
    try:
        value = json.loads(raw).get("input_types") if raw else None
    except (ValueError, AttributeError):
        return None
    return [str(t) for t in value] if isinstance(value, list) else None
