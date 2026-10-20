"""Assistant buttons and commands (DESIGN §14.12.4, §14.12.6, §14.16): follow-up taps, Retry, ``/title``
and ``/memory``.

* ``chat_followup`` (single-use, 24 h, bound to the answer message): the label must equal one the view
  rendered (its ``tg_views`` state); the echo ``↪️ <text>`` is sent **first**, then the text goes to
  Hermes as the next turn with ``origin='followup_tap'``. It never reaches the command registry.
* ``chat_retry``: the closed-relay and failed-turn messages offer it; it relays the same text again.
* ``/title <text>`` (and ``[✏️ Rename]``): ``editForumTopic`` of a user chat topic; ≤ 48 characters,
  sanitized, controls rejected.
* ``/memory``: what memory is, ``[🧠 What do you remember?]`` (a canned prompt), ``[🗑 Forget this chat]``
  (after a confirm tap: Hermes ``DELETE /api/sessions/{id}``) and ``[💬 New chat]``.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field
from telegram.error import TelegramError

from ossbot.gatekeeper import db
from ossbot.gatekeeper.telegram import chat_relay, routing
from ossbot.gatekeeper.telegram.actions import Tap, action
from ossbot.gatekeeper.telegram.commands.registry import CommandContext, Reply, command
from ossbot.gatekeeper.telegram.escape import esc, make_visible
from ossbot.gatekeeper.telegram.runtime import ui_of
from ossbot.gatekeeper.telegram.topics import session_key
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send
from ossbot.gatekeeper.telegram.ui.limits import TOPIC_NAME_MAX, label_len

log = logging.getLogger(__name__)
MEMORY_PROMPT = "What do you remember about me? List the facts you keep in memory."
MEMORY_TEXT = ("🧠 <b>Memory</b> · Hermes keeps memory across chats in every tier. "
               "/new starts a fresh conversation; memory stays.")  # fmt: skip
CONFIRM_TTL = timedelta(minutes=5)


class FollowupArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Annotated[str, Field(min_length=1, max_length=64)]
    turn_id: Annotated[int, Field(ge=1)]


class RetryArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Annotated[str, Field(min_length=1, max_length=4000)]
    reply_to: Annotated[int, Field(ge=0)]


class SessionArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: Annotated[str, Field(min_length=1, max_length=256)]


def _kind(tap: Tap) -> tuple[routing.ChatKind, str | None]:
    topic = ui_of(tap.app).topics.name_of(tap.thread_id)
    return routing.chat_kind(topic), topic


@action("chat_followup", arg=FollowupArg)
async def followup(tap: Tap) -> str:
    app, arg = tap.app, tap.arg
    view = db.one(app.conn, "SELECT state_json FROM tg_views WHERE message_id = ? AND chat_id = ?",
                  (tap.message_id, tap.chat_id))  # fmt: skip
    state = json.loads(view["state_json"] or "{}") if view is not None else {}
    if arg.text not in state.get("followups", []) or state.get("turn_id") != arg.turn_id:
        log.error("follow-up tap does not match its rendered label", extra={"event": "followup_mismatch"})
        return k.toast("Refused: this suggestion no longer matches its answer.")
    if app.chat is None:
        return k.toast("🤖 The assistant is not configured.")
    kind, topic = _kind(tap)
    key, _ = chat_relay.session_for(app, kind, tap.user_id, tap.thread_id)
    if tap.handlers.session_busy(key):
        return k.toast(chat_relay.STILL_ANSWERING)
    ui_send.deliver(app, k.plain("fyi", (k.Const("↪️ "), k.Text(arg.text, 64))), thread_id=tap.thread_id)
    await chat_relay.relay(app, tap.handlers, kind=kind, topic=topic, text=arg.text, user_id=tap.user_id,
                           thread_id=tap.thread_id, message_id=tap.message_id, origin="followup_tap")  # fmt: skip
    return ""


@action("chat_retry", arg=RetryArg)
async def retry(tap: Tap) -> str:
    kind, topic = _kind(tap)
    started = await chat_relay.relay(tap.app, tap.handlers, kind=kind, topic=topic, text=tap.arg.text,
                                     user_id=tap.user_id, thread_id=tap.thread_id,
                                     message_id=tap.arg.reply_to or tap.message_id, origin="retry")  # fmt: skip
    return "🔁 Retrying…" if started else ""


# ---------------------------------------------------------------------------------- /title
def clean_title(text: str) -> str | None:
    """A topic name: ≤ 48 characters, whitespace collapsed, no control/invisible characters."""
    _, hidden = make_visible(text)
    name = " ".join(text.split())
    if hidden or not name or label_len(name) > TOPIC_NAME_MAX:
        return None
    return name


async def rename_topic(app: Any, handlers: Any, thread_id: int | None, text: str) -> str:
    topics = ui_of(app).topics
    if topics.mode == "flat" or thread_id is None or topics.name_of(thread_id) is not None:
        return "✏️ Only a chat topic can be renamed (topic mode)."
    name = clean_title(text)
    if name is None:
        return f"✏️ A title is 1-{TOPIC_NAME_MAX} visible characters."
    try:
        await chat_relay.direct(handlers, "editForumTopic", {"chat_id": app.telegram_chat_id,
                                                             "message_thread_id": thread_id, "name": name})  # fmt: skip
    except TelegramError as exc:  # the Bot API refused: say so, never crash the command task
        log.warning("editForumTopic failed: %s", type(exc).__name__, extra={"event": "topic_rename_failed"})
        return "✏️ Telegram refused the new title."
    return f"✏️ Renamed to {esc(name)}"


@command("title", help="Rename this chat topic", safety="personal", effect="io")
async def title(ctx: CommandContext, args: list[str]) -> Reply:
    if not args:
        return Reply("Usage: /title &lt;new name&gt;")
    return Reply(await rename_topic(ctx.app, ctx.handlers, ctx.thread_id, " ".join(args)))


# ---------------------------------------------------------------------------------- /memory
def memory_view(app: Any, key: str) -> k.View:
    rows: k.Rows = [
        [k.button("🧠 What do you remember?", "memory_ask")],
        [k.button("🗑 Forget this chat", "memory_forget", {"key": key})],
        [k.button("💬 New chat", "memory_new")],
    ]
    return k.View(kind="fyi", blocks=[k.Block((k.Lines(((k.Html(MEMORY_TEXT),),)),))], rows=rows)


@command("memory", help="What Hermes remembers", safety="read")
async def memory(ctx: CommandContext, args: list[str]) -> Reply:
    app = ctx.app
    if app.chat is None:
        return Reply("🤖 The assistant is not configured.")
    key = session_key(ui_of(app).topics, ctx.tg_user_id, ctx.thread_id, app.chat.service.turns.session_key(
        ctx.tg_user_id))  # fmt: skip
    return Reply("", view=memory_view(app, key))


@action("memory_ask")
async def memory_ask(tap: Tap) -> str:
    kind, topic = _kind(tap)
    await chat_relay.relay(tap.app, tap.handlers, kind=kind, topic=topic, text=MEMORY_PROMPT, user_id=tap.user_id,
                           thread_id=tap.thread_id, message_id=tap.message_id, origin="typed")  # fmt: skip
    return ""


@action("memory_forget", arg=SessionArg)
async def memory_forget(tap: Tap) -> str:
    rows: k.Rows = [[k.button("🗑 Forget", "memory_forget_confirm", {"key": tap.arg.key}, style="danger",
                              single_use=True, ttl=CONFIRM_TTL),
                     k.button("↩️ Cancel", "memory_forget_cancel", single_use=True, ttl=CONFIRM_TTL)]]  # fmt: skip
    view = k.confirm_view("Forget this chat? Hermes deletes this conversation's session.", rows=rows)
    ui_send.deliver(tap.app, view, thread_id=tap.thread_id, reply_to=tap.message_id)
    return ""


@action("memory_forget_confirm", arg=SessionArg, effect="io")
async def memory_forget_confirm(tap: Tap) -> str:
    app = tap.app
    if app.chat is None:
        return "🤖 The assistant is not configured."
    client = getattr(app.chat.service.backend, "client", None)
    ok = False
    if client is not None and hasattr(client, "delete_session"):
        ok = await client.delete_session(tap.arg.key)
    text = "🗑 This chat was forgotten." if ok else "🗑 Hermes could not delete this chat's session."
    ui_send.edit(app, k.plain("fyi", k.line(text)), message_id=tap.message_id)
    return ""


@action("memory_forget_cancel")
async def memory_forget_cancel(tap: Tap) -> str:
    ui_send.edit(tap.app, k.plain("fyi", k.line("Nothing was deleted.")), message_id=tap.message_id)
    return ""


@action("memory_new")
async def memory_new(tap: Tap) -> str:
    if tap.app.chat is None:
        return "🤖 The assistant is not configured."
    tap.app.chat.service.turns.new_session(tap.user_id)
    return "💬 New chat: your next message starts fresh."
