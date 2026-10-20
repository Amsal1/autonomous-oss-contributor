"""Correlate Telegram's implicit topic creation with a command typed in "All" (ADR-0020 addendum,
post-v1 2026-10-21, second fix).

Live shape (journal ``tg_thread_fields``, 2026-10-20): a command typed in "All" of a private chat with
topics arrives as two updates about 300 ms apart. Update A (``message_id`` N) is the
``forum_topic_created`` service message of a NEW thread (``is_topic_message`` true, no text); update B
(``message_id`` N+1) is the command itself with no ``message_thread_id``. B carries nothing that points
at A, so the pair is proven only by this correlation:

- same chat and same sender, B's ``message_id`` == A's + 1, arrival gap <= ``TTL_S``;
- A's topic name equals B's command text (``/cmd@botname`` allowed) or A says ``is_name_implicit``.

Either order is handled: whichever side arrives first waits here (bounded, ``CAP`` entries per side,
``TTL_S`` lifetime) and the second one completes the pair. Nothing ever waits on this: the command is
answered first and the delete is queued when the pair completes, or the pending side is dropped on
expiry. A replayed update or a second pair for the same thread never yields a second match.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger(__name__)

TTL_S = 2.0  # the live gap is ~300 ms; anything slower is not proven
CAP = 64  # per pending side, and the replay and done guards hold CAP * 4
NAME_MAX = 128  # ForumTopicCreated.name is at most 128 characters

Order = Literal["topic_first", "command_first"]


@dataclass(frozen=True)
class TopicSeen:
    """Update A: a ``forum_topic_created`` in a thread the gatekeeper had never seen."""

    chat_id: int
    user_id: int
    message_id: int
    thread_id: int
    name: str
    implicit: bool
    at: float  # seconds (gatekeeper clock)


@dataclass(frozen=True)
class CommandSeen:
    """Update B: a command typed in "All" (no ``message_thread_id``, not a topic message)."""

    chat_id: int
    user_id: int
    message_id: int
    text: str
    at: float


@dataclass(frozen=True)
class Match:
    thread_id: int
    order: Order
    gap_ms: int
    name_matches_command: bool
    is_name_implicit: bool


def command_key(text: str) -> str:
    """``/Status@ossbot_bot  x`` -> ``/status x``: the command word lowercased without its bot suffix,
    whitespace collapsed, cut to the topic-name limit."""
    words = text.strip().split()
    if not words:
        return ""
    head = words[0]
    if head.startswith("/"):
        head = head.split("@", 1)[0].lower()
    return " ".join([head, *words[1:]])[:NAME_MAX]


def name_matches(name: str, text: str) -> bool:
    key = command_key(name)
    return key.startswith("/") and key == command_key(text)


class ImplicitTopics:
    def __init__(self, *, ttl_s: float = TTL_S, cap: int = CAP) -> None:
        self.ttl_s = ttl_s
        self.cap = cap
        self._topics: OrderedDict[tuple[int, int], TopicSeen] = OrderedDict()  # (chat, message_id)
        self._commands: OrderedDict[tuple[int, int], CommandSeen] = OrderedDict()
        self._seen: OrderedDict[tuple[str, int, int], None] = OrderedDict()  # replay guard
        self._done: OrderedDict[tuple[int, int], None] = OrderedDict()  # (chat, thread) already matched

    def pending(self) -> tuple[int, int]:
        return len(self._topics), len(self._commands)

    def on_topic(self, ev: TopicSeen) -> Match | None:
        self._purge(ev.at)
        if not self._first("topic", ev.chat_id, ev.message_id) or (ev.chat_id, ev.thread_id) in self._done:
            return None
        cmd = self._commands.get((ev.chat_id, ev.message_id + 1))
        if cmd is not None and self._pairs(ev, cmd):
            del self._commands[(cmd.chat_id, cmd.message_id)]
            return self._match(ev, cmd, "command_first")
        self._put(self._topics, (ev.chat_id, ev.message_id), ev)
        return None

    def on_command(self, ev: CommandSeen) -> Match | None:
        self._purge(ev.at)
        if not self._first("command", ev.chat_id, ev.message_id):
            return None
        topic = self._topics.get((ev.chat_id, ev.message_id - 1))
        if topic is not None and self._pairs(topic, ev) and (topic.chat_id, topic.thread_id) not in self._done:
            del self._topics[(topic.chat_id, topic.message_id)]
            return self._match(topic, ev, "topic_first")
        self._put(self._commands, (ev.chat_id, ev.message_id), ev)
        return None

    # ------------------------------------------------------------------ internals
    def _pairs(self, topic: TopicSeen, cmd: CommandSeen) -> bool:
        return (topic.chat_id == cmd.chat_id and topic.user_id == cmd.user_id
                and cmd.message_id == topic.message_id + 1 and abs(cmd.at - topic.at) <= self.ttl_s
                and (topic.implicit or name_matches(topic.name, cmd.text)))  # fmt: skip

    def _match(self, topic: TopicSeen, cmd: CommandSeen, order: Order) -> Match:
        self._put(self._done, (topic.chat_id, topic.thread_id), None, cap=self.cap * 4)
        return Match(topic.thread_id, order, round(abs(cmd.at - topic.at) * 1000),
                     name_matches(topic.name, cmd.text), topic.implicit)  # fmt: skip

    def _first(self, kind: str, chat_id: int, message_id: int) -> bool:
        key = (kind, chat_id, message_id)
        if key in self._seen:
            return False
        self._put(self._seen, key, None, cap=self.cap * 4)
        return True

    def _put[K, V](self, store: OrderedDict[K, V], key: K, value: V, *, cap: int | None = None) -> None:
        store[key] = value
        while len(store) > (cap or self.cap):
            store.popitem(last=False)

    def _purge(self, now: float) -> None:
        old_topics = [k for k, v in self._topics.items() if now - v.at > self.ttl_s]
        old_commands = [k for k, c in self._commands.items() if now - c.at > self.ttl_s]
        for key in old_topics:
            del self._topics[key]
        for key in old_commands:
            del self._commands[key]
        expired = len(old_topics) + len(old_commands)
        if expired:
            log.info("implicit topic correlation: %d pending side(s) expired", expired,
                     extra={"event": "implicit_topic_expired", "count": expired})  # fmt: skip
