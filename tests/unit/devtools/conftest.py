"""The demo renderer (``scripts/dev/demo``) is a dev tool outside ``src``: put it on ``sys.path``, plus a tiny
transcript builder for its tests."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

DEMO = Path(__file__).resolve().parents[3] / "scripts" / "dev" / "demo"
if str(DEMO) not in sys.path:
    sys.path.insert(0, str(DEMO))

AT = "2026-10-17T12:00:00.000000Z"


class Tx:
    """Builds transcript events with increasing ``seq``."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.next_id = 900

    def _add(self, event: dict[str, Any]) -> dict[str, Any]:
        event = {"seq": len(self.events) + 1, "at": AT, **event}
        self.events.append(event)
        return event

    def bot(self, method: str, params: dict[str, Any], result: Any = True, ok: bool = True) -> dict[str, Any]:
        return self._add({"type": "bot", "method": method, "params": params, "ok": ok, "result": result})

    def send(self, text: str, *, thread: int | None = 30, markup: Any = None, reply_to: int | None = None) -> int:
        mid = self.next_id
        self.next_id += 1
        params: dict[str, Any] = {"chat_id": 1, "text": text, "parse_mode": "HTML"}
        if thread is not None:
            params["message_thread_id"] = thread
        if markup is not None:
            params["reply_markup"] = markup
        if reply_to is not None:
            params["reply_parameters"] = {"message_id": reply_to}
        self.bot("sendMessage", params, {"message_id": mid})
        return mid

    def send_rich(self, blocks: list[Any], *, thread: int | None = 30) -> int:
        mid = self.next_id
        self.next_id += 1
        params: dict[str, Any] = {"chat_id": 1, "rich_message": {"blocks": blocks}}
        if thread is not None:
            params["message_thread_id"] = thread
        self.bot("sendRichMessage", params, {"message_id": mid})
        return mid

    def user(self, text: str, *, message_id: int = 500, thread: int | None = 30) -> int:
        self._add({"type": "user", "message_id": message_id, "thread": thread, "text": text})
        return message_id

    def mark(self, name: str, show: list[int], **kw: Any) -> None:
        self._add({"type": "mark", "name": name, "show": show, "pressed": None, "upto": None, "draft": None, **kw})

    def doc(self) -> dict[str, Any]:
        return {"version": 1, "events": self.events}


def keyboard(*rows: list[str], style: str | None = None) -> dict[str, Any]:
    out = []
    for row in rows:
        out.append([{"text": label, "callback_data": f"t:{i}", **({"style": style} if style and i == 0 else {})}
                    for i, label in enumerate(row)])  # fmt: skip
    return {"inline_keyboard": out}
