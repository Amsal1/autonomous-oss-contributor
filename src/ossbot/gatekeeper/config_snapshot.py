"""Config snapshot load and human-confirmed reload (DESIGN §7.9).

The four security files are read from drvfs only at start and on a confirmed reload. A reload
is two-step: ``prepare_reload`` returns the diff and the hash of what it read; ``apply_reload``
re-reads, requires the same hash, then in one transaction records the snapshot, repoints
``current``, updates ``control_state.config_hash``, syncs the monotonic floor and invalidates
every open approval (the hash is part of every binding as ``policy_hash``).
"""

from __future__ import annotations

import difflib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from ossbot.common.clock import Clock, to_iso
from ossbot.common.config import SNAPSHOT_FILES, ConfigBundle, ConfigError, parse_bundle
from ossbot.common.paths import Paths
from ossbot.common.snapshot import snapshot_hash, write_snapshot
from ossbot.gatekeeper import db, policy
from ossbot.gatekeeper.approvals import ApprovalService, Invalidated
from ossbot.gatekeeper.audit import AuditLog
from ossbot.gatekeeper.control import ControlService
from ossbot.gatekeeper.decisions import DecisionServices

MAX_CONFIG_FILE = 1 << 20


class ReloadConflict(RuntimeError):
    """409: the files changed between the diff and the confirm, or nothing changed."""


@dataclass(frozen=True)
class LoadedSnapshot:
    hash: str
    files: dict[str, bytes]
    bundle: ConfigBundle


@dataclass(frozen=True)
class ReloadPreview:
    hash: str
    diff: str
    changed_files: tuple[str, ...]


def read_config_files(paths: Paths) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for name in SNAPSHOT_FILES:
        path = paths.config_dir / name
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            continue
        if len(data) > MAX_CONFIG_FILE:
            raise ConfigError({name: ["file larger than 1 MiB"]})
        files[name] = data
    return files


def unified_diff(old: dict[str, bytes], new: dict[str, bytes]) -> tuple[str, tuple[str, ...]]:
    chunks: list[str] = []
    changed: list[str] = []
    for name in sorted(set(old) | set(new)):
        a = old.get(name, b"").decode("utf-8", "replace").splitlines(keepends=True)
        b = new.get(name, b"").decode("utf-8", "replace").splitlines(keepends=True)
        if a != b:
            changed.append(name)
            chunks.extend(difflib.unified_diff(a, b, f"a/config/{name}", f"b/config/{name}"))
    return "".join(chunks), tuple(changed)


class SnapshotManager:
    def __init__(
        self,
        conn: sqlite3.Connection,
        paths: Paths,
        clock: Clock,
        audit: AuditLog,
        control: ControlService,
        approvals: ApprovalService,
        notify: Callable[[str], None] | None = None,
        *,
        decisions: DecisionServices | None = None,
    ) -> None:
        self.conn = conn
        self.paths = paths
        self.clock = clock
        self.audit = audit
        self.control = control
        self.approvals = approvals
        self.decisions = decisions
        # extra load checks on a candidate bundle (e.g. the U14 secret scanner, BA-16); a ValueError
        # refuses the start or the /reload like an invalid file
        self.validators: list[Callable[[ConfigBundle], object]] = []
        self.notify = notify or (lambda text: None)
        self._current: LoadedSnapshot | None = None

    @property
    def current(self) -> LoadedSnapshot:
        if self._current is None:
            raise RuntimeError("config snapshot not loaded")
        return self._current

    def _invalidate_all(self, reason: str, actor: str) -> list[Invalidated]:
        """Both branches (startup reconcile and confirmed reload): all three decision services."""
        if self.decisions is None:
            return self.approvals.invalidate_all_open(reason, actor)
        return self.decisions.invalidate_all_open(reason, actor)

    def _validate(self, files: dict[str, bytes]) -> LoadedSnapshot:
        bundle = parse_bundle(files)
        for check in self.validators:
            try:
                check(bundle)
            except ConfigError:
                raise
            except ValueError as exc:
                raise ConfigError({"policy.yaml": [str(exc)]}) from exc
        return LoadedSnapshot(hash=snapshot_hash(files), files=files, bundle=bundle)

    def _record(self, snap: LoadedSnapshot, actor: str, diff: str, changed: tuple[str, ...]) -> None:
        """Caller holds the transaction."""
        self.conn.execute(
            "INSERT INTO config_snapshots (hash, files_json, loaded_at, loaded_by) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(hash) DO NOTHING",
            (
                snap.hash,
                json.dumps({k: v.decode("utf-8") for k, v in sorted(snap.files.items())}, sort_keys=True),
                to_iso(self.clock.now()),
                actor,
            ),
        )
        added = policy.sync_floor(self.conn, self.clock, snap.bundle.profile)
        self.audit.append(
            actor,
            "config_loaded",
            {"hash": snap.hash, "files": sorted(snap.files), "changed": list(changed), "diff": diff[:20000],
             "floor_added": [f"{k}:{v}" for k, v in added]},
        )  # fmt: skip

    def load_at_start(self) -> LoadedSnapshot:
        """Validate and snapshot the files; refuse to start on any error (fail closed)."""
        snap = self._validate(read_config_files(self.paths))
        write_snapshot(self.paths.snapshot_root, snap.files)
        with db.transaction(self.conn):
            created = self.control.initialize(snap.hash)
            previous = db.current_policy_hash(self.conn)
            changed: tuple[str, ...] = ()
            diff = ""
            if not created and previous != snap.hash:
                old = self._stored_files(previous)
                diff, changed = unified_diff(old, snap.files)
                self.control.set_config_hash(snap.hash)
                self._invalidate_all("config_snapshot_changed", "system")
            self._record(snap, "system", diff, changed)
            policy.check_startup(self.conn, snap.bundle.profile)
        self._current = snap
        return snap

    def _stored_files(self, digest: str) -> dict[str, bytes]:
        row = db.one(self.conn, "SELECT files_json FROM config_snapshots WHERE hash = ?", (digest,))
        if row is None:
            return {}
        return {k: str(v).encode("utf-8") for k, v in json.loads(row["files_json"]).items()}

    def prepare_reload(self) -> ReloadPreview:
        files = read_config_files(self.paths)
        self._validate(files)  # raises ConfigError -> 400 with the messages
        digest = snapshot_hash(files)
        diff, changed = unified_diff(self.current.files, files)
        return ReloadPreview(hash=digest, diff=diff, changed_files=changed)

    def apply_reload(self, confirm_hash: str, actor: str) -> tuple[LoadedSnapshot, list[Invalidated]]:
        files = read_config_files(self.paths)
        snap = self._validate(files)
        if snap.hash != confirm_hash:
            raise ReloadConflict("config files changed since the diff was shown; review the new diff")
        if snap.hash == self.current.hash:
            raise ReloadConflict("config unchanged")
        diff, changed = unified_diff(self.current.files, files)
        write_snapshot(self.paths.snapshot_root, snap.files)
        with db.transaction(self.conn):
            self.control.set_config_hash(snap.hash)
            invalidated = self._invalidate_all("config_snapshot_changed", actor)
            self._record(snap, actor, diff, changed)
            policy.check_startup(self.conn, snap.bundle.profile)
        self._current = snap
        self.notify(f"⚙️ Config reloaded by {actor}: {', '.join(changed)} (snapshot {snap.hash[:12]})")
        return snap, invalidated
