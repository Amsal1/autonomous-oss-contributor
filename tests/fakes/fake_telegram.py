"""Fake Bot API for python-telegram-bot: a ``BaseRequest`` that records calls and answers with
minimal valid objects, plus builders for synthetic ``Update`` payloads (DESIGN §22.2)."""

from __future__ import annotations

import itertools
import json
from typing import Any

from telegram.request import BaseRequest, RequestData

BOT_ID = 777000
FAKE_NOW = 1_792_238_400  # FakeClock's start, 2026-10-17T12:00:00Z (a message sent "now")
_update_ids = itertools.count(1)


class FakeBotRequest(BaseRequest):
    """Records every call. Bot API 10.x methods answer like the live API (sendRichMessage returns a
    Message, createForumTopic a ForumTopic, drafts True). ``fail(method, description)`` makes the next
    call of ``method`` answer 400 with that description (``code`` for other errors)."""

    def __init__(self, *, topics: bool = False, user_topics: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.files: list[tuple[str, str, bytes]] = []  # uploads: (method, filename, bytes)
        self.message_ids = itertools.count(900)
        self.thread_ids = itertools.count(30)
        self.topics = topics
        self.user_topics = user_topics
        self.fail_queue: dict[str, list[tuple[int, str]]] = {}
        self.file_bytes: dict[str, bytes] = {}  # file_id -> bytes served by getFile + download
        self.downloads: list[str] = []

    def fail(self, method: str, description: str, *, code: int = 400, times: int = 1) -> None:
        self.fail_queue.setdefault(method, []).extend([(code, description)] * times)

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
        if "/file/bot" in url:  # a getFile download (File.download_as_bytearray)
            self.downloads.append(endpoint)
            return 200, self.file_bytes[endpoint]
        params = dict(request_data.parameters) if request_data is not None else {}
        self.calls.append((endpoint, params))
        if request_data is not None and request_data.contains_files:
            for value in (request_data.multipart_data or {}).values():
                self.files.append((endpoint, str(value[0]), bytes(value[1])))  # (method, filename, bytes)
        queued = self.fail_queue.get(endpoint)
        if queued:
            code, description = queued.pop(0)
            return code, json.dumps({"ok": False, "error_code": code, "description": description}).encode()
        result: Any = True
        if endpoint == "getMe":
            result = {"id": BOT_ID, "is_bot": True, "first_name": "ossbot", "username": "ossbot_test_bot",
                      "can_join_groups": False, "can_read_all_group_messages": False,
                      "supports_inline_queries": False, "has_topics_enabled": self.topics,
                      "allows_users_to_create_topics": self.user_topics}  # fmt: skip
        elif endpoint in {"sendMessage", "editMessageText", "editMessageReplyMarkup", "sendRichMessage",
                          "sendDocument"}:  # fmt: skip
            message_id = params.get("message_id") or next(self.message_ids)
            result = {"message_id": message_id, "date": 1_760_000_000, "text": params.get("text", ""),
                      "chat": {"id": params.get("chat_id"), "type": "private"}}  # fmt: skip
            if params.get("message_thread_id"):
                result["message_thread_id"] = params["message_thread_id"]
        elif endpoint == "getFile":
            fid = str(params.get("file_id"))
            result = {"file_id": fid, "file_unique_id": f"u{fid}", "file_size": len(self.file_bytes.get(fid, b"")),
                      "file_path": f"files/{fid}"}  # fmt: skip
        elif endpoint == "createForumTopic":
            result = {"message_thread_id": next(self.thread_ids), "name": params.get("name", ""),
                      "icon_color": params.get("icon_color", 0)}  # fmt: skip
        return 200, json.dumps({"ok": True, "result": result}).encode()


def message_payload(req: FakeBotRequest, method: str = "sendMessage") -> list[dict[str, Any]]:
    return [p for m, p in req.calls if m == method]


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


def message_update(from_id: int, chat_id: int, text: str, chat_type: str = "private", *,
                   thread_id: int | None = None, reply_to: int | None = None, date: int = FAKE_NOW,
                   forward: bool = False, message_id: int | None = None, quote: str | None = None,
                   quote_manual: bool = True, is_topic: bool = True,
                   topic_created: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    """``is_topic=False`` drops ``is_topic_message`` while keeping ``message_thread_id``; ``topic_created``
    (a ``ForumTopicCreated``) makes ``reply_to_message`` the topic's creation service message."""
    message: dict[str, Any] = {
        "message_id": message_id or next(_update_ids),
        "date": date,
        "chat": chat(chat_id, chat_type),
        "from": user(from_id),
        "text": text,
    }
    if thread_id is not None:
        message["message_thread_id"] = thread_id
        if is_topic:
            message["is_topic_message"] = True
    if topic_created is not None and reply_to is None and thread_id is not None:
        message["reply_to_message"] = {"message_id": thread_id, "date": date, "chat": chat(chat_id, chat_type),
                                       "from": user(from_id), "message_thread_id": thread_id,
                                       "is_topic_message": True, "forum_topic_created": topic_created}  # fmt: skip
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to, "date": date, "chat": chat(chat_id, chat_type),
                                       "from": {"id": BOT_ID, "is_bot": True, "first_name": "ossbot"},
                                       "text": "a bot message"}  # fmt: skip
    if quote is not None:  # Bot API TextQuote (a reply with quote)
        message["quote"] = {"text": quote, "position": 0, "is_manual": quote_manual}
    if forward:
        message["forward_origin"] = {"type": "hidden_user", "sender_user_name": "someone", "date": date}
    return {"update_id": next(_update_ids), "message": message}


def media_update(from_id: int, chat_id: int, *, photo: str | None = None, document: dict[str, Any] | None = None,
                 voice: bool = False, location: tuple[float, float] | None = None, caption: str = "",
                 media_group_id: str | None = None, thread_id: int | None = None,
                 message_id: int | None = None) -> dict[str, Any]:  # fmt: skip
    """A photo (``file_id``; three sizes, the largest 2560 px), a document (``file_id``, ``mime_type``,
    ``file_name``, ``file_size``), a voice note or a location, with an optional caption and album id."""
    message: dict[str, Any] = {"message_id": message_id or next(_update_ids), "date": FAKE_NOW,
                               "chat": chat(chat_id), "from": user(from_id)}  # fmt: skip
    if photo is not None:
        message["photo"] = [
            {"file_id": f"{photo}-s", "file_unique_id": f"{photo}-us", "width": 320, "height": 240, "file_size": 9},
            {"file_id": photo, "file_unique_id": f"{photo}-u", "width": 1280, "height": 960},
            {"file_id": f"{photo}-x", "file_unique_id": f"{photo}-ux", "width": 2560, "height": 1920},
        ]
    if document is not None:
        message["document"] = {"file_unique_id": f"u{document['file_id']}", **document}
    if voice:
        message["voice"] = {"file_id": "voice1", "file_unique_id": "uvoice1", "duration": 3}
    if location is not None:
        message["location"] = {"latitude": location[0], "longitude": location[1]}
    if caption:
        message["caption"] = caption
    if media_group_id is not None:
        message["media_group_id"] = media_group_id
    if thread_id is not None:
        message["message_thread_id"] = thread_id
        message["is_topic_message"] = True
    return {"update_id": next(_update_ids), "message": message}


def stopped_update(chat_id: int, draft_id: int, chat_type: str = "private") -> dict[str, Any]:
    """Bot API 10.3 ``stopped_message_generation`` (no ``from``)."""
    return {"update_id": next(_update_ids),
            "stopped_message_generation": {"chat": chat(chat_id, chat_type), "draft_id": draft_id}}  # fmt: skip
