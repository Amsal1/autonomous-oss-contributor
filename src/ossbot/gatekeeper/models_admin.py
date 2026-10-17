"""Model switch writer and catalog (DESIGN §16, N2, N6, R4-N3).

The gatekeeper is the only writer of ``config/models.yaml``: ruamel round-trip keeps comments,
the new content is validated before an atomic ``os.replace`` of a same-directory temp file.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sqlite3
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
    def __init__(self, path: Path, conn: sqlite3.Connection, clock: Clock, audit: AuditLog) -> None:
        self.path = path
        self.conn = conn
        self.clock = clock
        self.audit = audit

    def read(self) -> ModelsConfig:
        return parse_models(self.path.read_bytes())

    # ----------------------------------------------------------------- catalog
    def catalog(self) -> list[dict[str, Any]]:
        """The latest catalog (rows seen in the newest refresh) plus forced ids."""
        newest = self.conn.execute("SELECT MAX(last_seen) FROM model_catalog WHERE forced = 0").fetchone()[0]
        rows = self.conn.execute(
            "SELECT model_id, display_name, forced FROM model_catalog WHERE forced = 1 OR last_seen = ?"
            " ORDER BY model_id",
            (newest,),
        ).fetchall()
        return [{"model_id": r["model_id"], "name": r["display_name"], "forced": bool(r["forced"])} for r in rows]

    def in_catalog(self, model_id: str) -> bool:
        return any(entry["model_id"] == model_id for entry in self.catalog())

    def upsert_catalog(self, models: list[CatalogEntry], actor: str) -> list[str]:
        now = to_iso(self.clock.now())
        new_ids: list[str] = []
        with db.transaction(self.conn):
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
        return new_ids

    # ------------------------------------------------------------------ writer
    def set_model(self, role: str, model: str, effort: str | None, force: bool, actor: str) -> str:
        if role != "default" and role not in ROLE_NAMES:
            raise ModelSetError(f"unknown role {role}")
        if not re.fullmatch(MODEL_ID_RE, model):
            raise ModelSetError("model id does not match the allowed pattern")
        if model == "default":
            if role == "default":
                raise ModelSetError("the default model cannot reference itself")
            # 'default' is a valid role reference
        elif force:
            with db.transaction(self.conn):
                now = to_iso(self.clock.now())
                self.conn.execute(
                    "INSERT INTO model_catalog (model_id, first_seen, last_seen, forced, forced_by, forced_at)"
                    " VALUES (?, ?, ?, 1, ?, ?) ON CONFLICT(model_id) DO UPDATE SET forced = 1,"
                    " forced_by = excluded.forced_by, forced_at = excluded.forced_at",
                    (model, now, now, actor, now),
                )
                self.audit.append(actor, "model_forced", {"model_id": model})
        elif not self.in_catalog(model):
            raise ModelSetError(f"{model} is not in the current Kiro model catalog")
        old_text = self.path.read_text(encoding="utf-8")
        data = load_roundtrip(old_text)
        node = data["default"] if role == "default" else data["roles"][role]
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
        os.replace(tmp, self.path)
        diff = "".join(
            difflib.unified_diff(old_text.splitlines(True), new_text.splitlines(True), "a/models.yaml", "b/models.yaml")
        )
        with db.transaction(self.conn):
            self.audit.append(actor, "model_set", {"role": role, "model": model, "effort": effort, "force": force})
        return diff
