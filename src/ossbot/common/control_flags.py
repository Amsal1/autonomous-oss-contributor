"""Kill/pause flag files in the control dir (DESIGN §13.2), shared by every layer.

``killed`` is JSON ``{level: soft|full, reason, actor, ts}``; a missing or unparseable level is
treated as ``full`` (fail closed). ``paused`` is JSON ``{reason, actor, ts}``; any content counts.
Runner (before each stage), sandboxd (before each job), the Docker filter (create/start) and the
gatekeeper (transitions, every outbound write) all call ``require_running``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

KillLevel = Literal["soft", "full"]
FLAG_MAX_BYTES = 4096


@dataclass(frozen=True)
class KillInfo:
    level: KillLevel
    reason: str
    actor: str
    ts: str


@dataclass(frozen=True)
class PauseInfo:
    reason: str
    actor: str
    ts: str


@dataclass(frozen=True)
class FlagState:
    killed: KillInfo | None
    paused: PauseInfo | None

    @property
    def running(self) -> bool:
        return self.killed is None and self.paused is None

    @property
    def state(self) -> Literal["running", "paused", "killed"]:
        if self.killed is not None:
            return "killed"
        if self.paused is not None:
            return "paused"
        return "running"


class Blocked(RuntimeError):
    """Raised when work must not start because the system is paused or killed (HTTP 423)."""

    def __init__(self, what: str, flags: FlagState) -> None:
        self.what = what
        self.flags = flags
        reason = flags.killed.reason if flags.killed else flags.paused.reason if flags.paused else ""
        super().__init__(f"{what} refused: {flags.state} ({reason})")


def _read_json(path: Path) -> dict[str, Any] | None:
    """Return the parsed object, ``{}`` for present-but-unparseable, or None if absent."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        return {}  # e.g. a symlink planted in place of the flag: present, unparseable
    with os.fdopen(fd, "rb") as fh:
        raw = fh.read(FLAG_MAX_BYTES + 1)
    try:
        data = json.loads(raw[:FLAG_MAX_BYTES].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def read_flags(control_dir: Path) -> FlagState:
    killed_raw = _read_json(control_dir / "killed")
    paused_raw = _read_json(control_dir / "paused")
    killed = None
    if killed_raw is not None:
        level = killed_raw.get("level")
        killed = KillInfo(
            level="soft" if level == "soft" else "full",
            reason=str(killed_raw.get("reason", "unparseable_flag")),
            actor=str(killed_raw.get("actor", "unknown")),
            ts=str(killed_raw.get("ts", "")),
        )
    paused = None
    if paused_raw is not None:
        paused = PauseInfo(
            reason=str(paused_raw.get("reason", "unparseable_flag")),
            actor=str(paused_raw.get("actor", "unknown")),
            ts=str(paused_raw.get("ts", "")),
        )
    return FlagState(killed=killed, paused=paused)


def require_running(control_dir: Path, what: str) -> FlagState:
    flags = read_flags(control_dir)
    if not flags.running:
        raise Blocked(what, flags)
    return flags


def write_flag(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write a flag file with mode 0640 (writers: gatekeeper and root only)."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)
