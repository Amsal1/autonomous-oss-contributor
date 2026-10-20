"""System topics in the owner's private chat, with the flat fallback (DESIGN §14.10.1, D1, AC 12).

At each start ``bootstrap`` reads ``getMe``: when ``has_topics_enabled`` (Bot API 9.3; the user turns
on "Threaded mode" in @BotFather → Bot Settings → Threads Settings) and ``telegram.yaml topics: auto``,
the four system topics are created
once (``createForumTopic``) and their ids kept in ``tg_topics`` across restarts. Otherwise, or after a
topic could not be re-created, ossbot runs in **flat mode**: no thread ids, and every message routed
to a system topic starts with that topic's section icon.

Outbox rows name their topic (``thread_key``); ``prepare`` resolves it right before the send, so a
"thread not found" error can re-create the topic once and re-send (``on_thread_not_found``).
Topic routing never changes authorization (U10).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from ossbot.common.clock import to_iso
from ossbot.gatekeeper import db
from ossbot.gatekeeper.outbox import OutboxRow, SendError
from ossbot.gatekeeper.telegram.rawapi import ForumTopicResult, MeResult
from ossbot.gatekeeper.telegram.ui.icons import SECTION_ICON, SYSTEM_TOPICS

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

log = logging.getLogger(__name__)
Call = Callable[[str, dict[str, Any]], Awaitable[Any]]
Mode = Literal["topics", "flat"]
SEND_METHODS = frozenset({"sendMessage", "sendRichMessage", "sendDocument", "sendPhoto", "sendMessageDraft",
                          "sendRichMessageDraft"})  # fmt: skip
THREAD_NOT_FOUND = "thread not found"


class Topics:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.mode: Mode = "flat"
        self.allows_user_topics = False
        self._recreated: set[str] = set()

    # ------------------------------------------------------------------ bootstrap
    async def bootstrap(self, call: Call) -> Mode:
        """Decide the mode for this run and ensure the system topics exist (created once)."""
        self._recreated.clear()
        if self.ctx.config.telegram.topics == "off":
            return self._flat("topics: off in telegram.yaml")
        try:
            me = MeResult.model_validate(await call("getMe", {}))
        except (SendError, ValueError) as exc:  # network or shape: flat for this start, re-checked next start
            return self._flat(f"getMe failed: {type(exc).__name__}")
        self.allows_user_topics = me.allows_users_to_create_topics
        if not me.has_topics_enabled:
            return self._flat("Threaded mode is off in @BotFather (has_topics_enabled false)")
        for name in SYSTEM_TOPICS:
            if self.thread_id_raw(name) is None and await self._create(name, call) is None:
                return self._flat(f"createForumTopic failed for {name}")
        self.mode = "topics"
        return self.mode

    def _flat(self, why: str) -> Mode:
        log.warning("telegram flat mode: %s", why, extra={"event": "telegram_flat_mode"})
        self.mode = "flat"
        return self.mode

    async def _create(self, name: str, call: Call) -> int | None:
        title, color = SYSTEM_TOPICS[name]
        payload = {"chat_id": self.ctx.telegram_chat_id, "name": title, "icon_color": color}
        try:
            result = ForumTopicResult.model_validate(await call("createForumTopic", payload))
        except (SendError, ValueError) as exc:
            log.warning("createForumTopic %s failed: %s", name, type(exc).__name__,
                        extra={"event": "telegram_topic_failed"})  # fmt: skip
            return None
        with db.transaction(self.ctx.conn):
            self.ctx.conn.execute(
                "INSERT INTO tg_topics (name, thread_id, created_at) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE"
                " SET thread_id = excluded.thread_id, created_at = excluded.created_at",
                (name, result.message_thread_id, to_iso(self.ctx.clock.now())),
            )
        return result.message_thread_id

    # ------------------------------------------------------------------ lookups
    def thread_id_raw(self, name: str) -> int | None:
        row = db.one(self.ctx.conn, "SELECT thread_id FROM tg_topics WHERE name = ?", (name,))
        return None if row is None else int(row["thread_id"])

    def thread_id(self, name: str | None) -> int | None:
        """The ``message_thread_id`` to send to (None in flat mode or for no topic)."""
        if name is None or self.mode == "flat":
            return None
        return self.thread_id_raw(name)

    def name_of(self, thread_id: int | None) -> str | None:
        """System topic name of a thread id; None = a user topic (an assistant conversation) or flat."""
        if thread_id is None or self.mode == "flat":
            return None
        row = db.one(self.ctx.conn, "SELECT name FROM tg_topics WHERE thread_id = ?", (thread_id,))
        return None if row is None else str(row["name"])

    # ------------------------------------------------------------------ send-time
    def prepare(self, row: OutboxRow) -> OutboxRow:
        """Resolve the row's topic; in flat mode prefix the section icon (§14.10.1)."""
        if row.thread_key is None or row.method not in SEND_METHODS | {"editMessageText"}:
            return row
        payload = dict(row.payload)
        thread = self.thread_id(row.thread_key)
        if row.method in SEND_METHODS:
            if thread is not None:
                payload["message_thread_id"] = thread
            else:
                payload.pop("message_thread_id", None)
        if thread is None and isinstance(payload.get("text"), str):
            icon = SECTION_ICON.get(row.thread_key, "")
            if icon and not str(payload["text"]).startswith(icon):
                payload["text"] = f"{icon} {payload['text']}"
        return replace(row, payload=payload)

    async def on_thread_not_found(self, row: OutboxRow, call: Call) -> bool:
        """Re-create the topic once per start and re-send; the second time switch to flat mode."""
        name = row.thread_key
        if name is None:
            return False
        if name not in self._recreated and self.mode == "topics":
            self._recreated.add(name)
            if await self._create(name, call) is not None:
                self.ctx.outbox.requeue(row, row.method, _without_thread(row.payload), thread_key=name)
                return True
        self._flat(f"topic {name} not found after a re-create")
        self.ctx.outbox.requeue(row, row.method, _without_thread(row.payload), thread_key=name)
        return True


def _without_thread(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    out.pop("message_thread_id", None)
    return out


def session_key(topics: Topics, user_id: int, thread_id: int | None, flat_key: str) -> str:
    """Hermes session of a thread (§14.10.1, §14.12.6): Assistant/user topics ``tg-<uid>-t<id>``,
    Reviews ``tg-<uid>-reviews``, flat mode ``tg-<uid>-<n>``."""
    if topics.mode == "flat" or thread_id is None:
        return flat_key
    if topics.name_of(thread_id) == "reviews":
        return f"tg-{user_id}-reviews"
    return f"tg-{user_id}-t{thread_id}"
