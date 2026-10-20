"""Audit anchors (DESIGN §12.7, N7).

Root appends ``{"seq":…, "row_hash":…, "ts":…}`` lines to ``audit-anchors.log`` (``chattr +a``)
every 5 minutes (watchdog) and whenever the gatekeeper connects to ``/run/ossbot/anchor.sock``
after an outbound write. The request payload is ignored by root; the gatekeeper only triggers.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from datetime import datetime
from pathlib import Path

from ossbot.common.clock import from_iso
from ossbot.gatekeeper.audit import Anchor

log = logging.getLogger(__name__)
MAX_ANCHOR_LINE = 4096


def parse_anchors(path: Path) -> tuple[list[Anchor], list[str]]:
    """Return (anchors, errors). A missing file is no anchors; malformed lines are errors."""
    anchors: list[Anchor] = []
    errors: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return anchors, errors
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            if len(line) > MAX_ANCHOR_LINE:
                raise ValueError("line too long")
            doc = json.loads(line)
            anchors.append(Anchor(seq=int(doc["seq"]), row_hash=str(doc["row_hash"]), ts=str(doc["ts"])))
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"anchors line {number}: {exc}")
    return anchors, errors


def last_anchor_time(path: Path) -> datetime | None:
    """The ``ts`` of the newest anchor line (reads only the file's tail); None when there is none."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 2 * MAX_ANCHOR_LINE))
            tail = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(tail):
        try:
            doc = json.loads(line)
            return from_iso(str(doc["ts"]))
        except (ValueError, KeyError, TypeError):
            continue
    return None


class AnchorClient:
    """Triggers a root anchor. Never raises: anchoring is defence in depth, not a write gate."""

    def __init__(self, sock_path: Path, timeout: float = 2.0) -> None:
        self.sock_path = sock_path
        self.timeout = timeout

    def trigger(self) -> bool:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(str(self.sock_path))
                sock.sendall(b"\n")
                reply = sock.recv(16)
        except OSError as exc:
            log.warning("audit anchor trigger failed: %s", exc, extra={"event": "anchor_trigger_failed"})
            return False
        ok = reply.strip() == b"OK"
        if not ok:
            log.warning("audit anchor trigger: unexpected reply %r", reply, extra={"event": "anchor_trigger_failed"})
        return ok
