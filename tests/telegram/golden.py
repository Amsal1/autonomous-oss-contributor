"""Golden files under tests/telegram/golden/. ``OSSBOT_UPDATE_GOLDENS=1`` rewrites them (review the diff)."""

from __future__ import annotations

import os
from pathlib import Path

GOLDEN = Path(__file__).resolve().parent / "golden"


def check(name: str, text: str) -> None:
    path = GOLDEN / name
    if os.environ.get("OSSBOT_UPDATE_GOLDENS") == "1" or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        if os.environ.get("OSSBOT_UPDATE_GOLDENS") != "1":
            raise AssertionError(f"golden {name} was missing and has been written; re-run and review it")
        return
    assert path.read_text(encoding="utf-8") == text, f"golden {name} differs (OSSBOT_UPDATE_GOLDENS=1 to accept)"
