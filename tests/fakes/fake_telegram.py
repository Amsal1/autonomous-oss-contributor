"""Fake Bot API for python-telegram-bot: a ``BaseRequest`` that records calls and answers with
minimal valid objects, plus builders for synthetic ``Update`` payloads (DESIGN §22.2)."""

from __future__ import annotations

import itertools
import json
from typing import Any

from telegram.request import BaseRequest, RequestData

BOT_ID = 777000
_update_ids = itertools.count(1)


class FakeBotRequest(BaseRequest):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.message_ids = itertools.count(900)

    @property
    def read_timeout(self) -> float | None:
        return 5.0

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def last(self, method: str) -> dict[str, Any]:
        return next(p for m, p in reversed(self.calls) if m == method)

    async def do_request(
        self,
        url: str,
        method: str,
        request_data: RequestData | None = None,
        read_timeout: Any = None,
        write_timeout: Any = None,
        connect_timeout: Any = None,
        pool_timeout: Any = None,
    ) -> tuple[int, bytes]:
        endpoint = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data is not None else {}
        self.calls.append((endpoint, params))
        result: Any = True
        if endpoint == "getMe":
            result = {"id": BOT_ID, "is_bot": True, "first_name": "ossbot", "username": "ossbot_test_bot",
                      "can_join_groups": False, "can_read_all_group_messages": False,
                      "supports_inline_queries": False}  # fmt: skip
        elif endpoint in {"sendMessage", "editMessageText", "editMessageReplyMarkup"}:
            message_id = params.get("message_id") or next(self.message_ids)
            result = {"message_id": message_id, "date": 1_760_000_000, "text": params.get("text", ""),
                      "chat": {"id": params.get("chat_id"), "type": "private"}}  # fmt: skip
        return 200, json.dumps({"ok": True, "result": result}).encode()


def user(uid: int) -> dict[str, Any]:
    return {"id": uid, "is_bot": False, "first_name": "Owner"}


def chat(cid: int, kind: str = "private") -> dict[str, Any]:
    doc: dict[str, Any] = {"id": cid, "type": kind}
    if kind != "private":
        doc["title"] = "a group"
    return doc


def callback_update(
    from_id: int, chat_id: int, message_id: int, data: str, chat_type: str = "private"
) -> dict[str, Any]:
    return {
        "update_id": next(_update_ids),
        "callback_query": {
            "id": f"cb{next(_update_ids)}",
            "from": user(from_id),
            "chat_instance": "ci",
            "data": data,
            "message": {
                "message_id": message_id,
                "date": 1_760_000_000,
                "chat": chat(chat_id, chat_type),
                "text": "card",
            },
        },
    }


def message_update(from_id: int, chat_id: int, text: str, chat_type: str = "private") -> dict[str, Any]:
    return {
        "update_id": next(_update_ids),
        "message": {
            "message_id": next(_update_ids),
            "date": 1_760_000_000,
            "chat": chat(chat_id, chat_type),
            "from": user(from_id),
            "text": text,
        },
    }
