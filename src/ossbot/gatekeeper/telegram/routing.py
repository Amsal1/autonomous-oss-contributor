"""The update routing table (DESIGN §14.10.4, normative). First match wins:

1. ``callback_query`` -> the token handler.
2. ``stopped_message_generation`` -> stop the stream with that ``draft_id`` (``authorized_chat_only``).
3. A reply to an open ``pending_input`` prompt (matched only by ``reply_to_message.message_id``) -> that
   input kind. Before the command rule, so a comment starting with ``/`` is a comment.
4. Text starting with ``/`` -> the command registry, in any topic.
5. A reply to a gatekeeper view, resolved through the view's own ``tg_views`` row (never its text).
6. A non-reply message with GitHub issue/PR/repo links -> link cards (≤ 3); when the message is only
   the URL(s) routing stops, otherwise it continues with 7-9.
7. Free text / media typed in ✅ Reviews -> Hermes session ``tg-<uid>-reviews``, answered in Reviews.
8. ... typed in 🚦 Control or 📈 OSS feed -> Hermes in the 💬 Assistant session, answered there.
9. Everything else -> Hermes in the session of its topic (Assistant, a user topic, or flat).

Classification only reads the gatekeeper's own tables; authorization happened before (U10).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Literal

from ossbot.gatekeeper import db
from ossbot.gatekeeper.telegram.pending_input import PendingInputs, Prompt
from ossbot.gatekeeper.telegram.topics import Topics
from ossbot.gatekeeper.telegram.ui.sanitize import MAX_LINK_CARDS, GithubRef, github_urls, link_notes, strip_urls

RouteKind = Literal["callback", "stop_generation", "pending_input", "command", "view_reply", "link_card",
                    "reviews_chat", "system_chat", "chat", "ignore"]  # fmt: skip
ChatKind = Literal["reviews_chat", "system_chat", "chat"]
REVIEW_VIEW_KINDS = frozenset({"file", "explorer", "interdiff", "card", "body_part", "commit_msg"})


@dataclass(frozen=True)
class Incoming:
    """What routing needs from an authorized update (built by the handler from the PTB ``Update``)."""

    kind: Literal["callback", "stopped", "message"]
    chat_id: int
    text: str = ""
    thread_id: int | None = None
    reply_to_message_id: int | None = None
    reply_to_from_bot: bool = False
    forwarded: bool = False
    entity_urls: tuple[str, ...] = ()
    has_media: bool = False
    date: int = 0  # unix seconds of message.date


@dataclass(frozen=True)
class Route:
    kind: RouteKind
    prompt: Prompt | None = None
    view: sqlite3.Row | None = None
    links: tuple[GithubRef, ...] = ()
    then: ChatKind | None = None  # rule 6: where the rest of the message goes (None = stop)
    topic: str | None = None  # system topic name of the originating thread (None = user topic / flat)
    notes: str = ""
    extra: dict[str, str] = field(default_factory=dict)


def chat_kind(topic: str | None) -> ChatKind:
    if topic == "reviews":
        return "reviews_chat"
    if topic in {"control", "feed"}:
        return "system_chat"
    return "chat"


def classify(conn: sqlite3.Connection, topics: Topics, pending: PendingInputs, inc: Incoming) -> Route:
    if inc.kind == "callback":
        return Route("callback")
    if inc.kind == "stopped":
        return Route("stop_generation")
    topic = topics.name_of(inc.thread_id)
    # 3. ForceReply correlation (by message id only, before the command rule)
    prompt = pending.peek(inc.chat_id, inc.reply_to_message_id)
    if prompt is not None:
        return Route("pending_input", prompt=prompt, topic=topic)
    # 4. commands, in any topic
    if inc.text.lstrip().startswith("/"):
        return Route("command", topic=topic)
    # 5. a reply to a gatekeeper view (its own tg_views row)
    if inc.reply_to_message_id is not None:
        view = db.one(conn, "SELECT * FROM tg_views WHERE message_id = ? AND chat_id = ?",
                      (inc.reply_to_message_id, inc.chat_id))  # fmt: skip
        if view is not None and (view["kind"] in REVIEW_VIEW_KINDS or view["kind"] in {"proposal", "skill_card",
                                                                                        "skill_part"}):  # fmt: skip
            return Route("view_reply", view=view, topic=topic)
    # 6. GitHub links in a non-reply message (forwards included)
    if inc.reply_to_message_id is None:
        found = github_urls(inc.text, list(inc.entity_urls), limit=None)
        if found:
            rest = strip_urls(inc.text)
            then = None if not rest and not inc.has_media else chat_kind(topic)
            more = {"more": str(len(found) - MAX_LINK_CARDS)} if len(found) > MAX_LINK_CARDS else {}
            return Route("link_card", links=tuple(found[:MAX_LINK_CARDS]), then=then, topic=topic,
                         notes=link_notes(inc.text, forwarded=inc.forwarded), extra=more)  # fmt: skip
    if not inc.text.strip() and not inc.has_media:
        return Route("ignore", topic=topic)
    # 7-9. the assistant
    return Route(chat_kind(topic), topic=topic)
