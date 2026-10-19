"""Reader side of the root Hermes-side Kiro probe result (DESIGN §15.2).

The root probe (``ossbot.ctl.hermes_probe``) writes ``/var/lib/ossbot/probes/hermes-kiro.json``
(root:gatekeeper 0640); the gatekeeper only reads it for the relay gate and the selftest. Keeping the
reader here means the gatekeeper never imports the root ACP client.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RESULT_PATH = Path("/var/lib/ossbot/probes/hermes-kiro.json")
MAX_AGE_S = 2 * 3600
MAX_BYTES = 65536


def read_result(path: Path = RESULT_PATH) -> dict[str, Any] | None:
    """The probe document, or None when missing, a symlink, unparseable or not an object."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(MAX_BYTES)
    try:
        doc = json.loads(data)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def relay_open(doc: Mapping[str, Any] | None, *, now: float | None = None, max_age_s: int = MAX_AGE_S) -> bool:
    """The relay gate on the probe file: ``passed: true`` and ``checked_at`` younger than 2 h."""
    if not doc or doc.get("passed") is not True or not isinstance(doc.get("checked_at"), str):
        return False
    try:
        checked = datetime.strptime(doc["checked_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return False
    age = (time.time() if now is None else now) - checked
    return -300 <= age < max_age_s


__all__ = ["MAX_AGE_S", "RESULT_PATH", "read_result", "relay_open"]
