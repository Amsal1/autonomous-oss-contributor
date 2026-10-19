"""Routing rules 7-9 (DESIGN §14.10.4): free text to Hermes, answered where it belongs (U6, U12).

* ✅ Reviews (rule 7): session ``tg-<uid>-reviews``; the answer is a classic message whose first line is
  the gatekeeper header ``🤖 Assistant · answer to your message`` and whose whole body is inside one
  ``<blockquote expandable>`` (cut at 3,500 with a pointer; the full answer goes to 💬 Assistant). The
  only button is ``[📝 Use as comment]``, offered when exactly one approval is open and it is an
  ``open_pr``/``push_update``.
* 🚦 Control / 📈 OSS feed (rule 8): answered in 💬 Assistant with ``↪️ from …``; the origin gets one
  silent ``🤖 Answered in 💬 Assistant`` reply.
* Everything else (rule 9): the topic's own session. In flat mode every answer carries the header and
  the quote, so it can never look like a card (U6, U12).

Each turn is a spawned ``chat_turn`` task with the session key (``Handlers.spawn``); a second message
to a busy session gets ``Still answering``. A closed relay answers with its reason and a Retry button.
The streaming draft UX is PLAN item 49 (FEAT-010).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from ossbot.gatekeeper.chat.backend import RelayClosed
from ossbot.gatekeeper.telegram.routing import ChatKind
from ossbot.gatekeeper.telegram.runtime import ui_of
from ossbot.gatekeeper.telegram.topics import session_key
from ossbot.gatekeeper.telegram.ui import icons
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send
from ossbot.gatekeeper.telegram.ui.limits import chunk_verbatim, cut

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

log = logging.getLogger(__name__)
STILL_ANSWERING = "⏳ Still answering — tap ⏹ Stop or wait"
HEADER = "🤖 Assistant · answer to your message"
ORIGIN_NAMES = {"control": "🚦 Control", "feed": "📈 OSS feed"}
USER_TEXT_MAX = 4000


# escaped characters per message; defusing can add one character per two ("/a/a…"), so 2,400 stays under
# the 3,900 budget with the header and quote tags
ANSWER_CHUNK = 2400


def chunks(answer: str) -> list[str]:
    """A long answer as several messages, split at line boundaries (never inside a message's budget)."""
    return chunk_verbatim(answer, ANSWER_CHUNK)


def quoted_answer(header: str, body: str, *, extra: k.Line | None = None) -> list[k.Block]:
    """Header outside, the whole body in one expandable quote (flat mode, Reviews; U6)."""
    lines = tuple((k.Text(ln),) for ln in body.splitlines() or [""])
    first: list[k.Line] = [(k.Const(header),)]
    if extra is not None:
        first.append(extra)
    return [k.Block((k.Lines(tuple(first)), k.Quote(lines, expandable=True)), name="answer")]


def plain_answer(body: str, *, extra: k.Line | None = None) -> list[k.Block]:
    lines = tuple((k.Text(ln),) for ln in body.splitlines() or [""])
    head: tuple[k.Line, ...] = (extra,) if extra is not None else ()
    return [k.Block((k.Lines(head + lines),), name="answer")]


def _single_open_review_action(ctx: AppContext) -> tuple[str, str, int | None] | None:
    """(cid, action_id, rev) when exactly one approval is open and it is an open_pr/push_update."""
    rows = ctx.conn.execute(
        "SELECT o.id, o.cid, o.kind, o.payload_json FROM approvals a JOIN outbound_actions o ON o.id = a.action_id"
        " WHERE a.status IN ('pending','armed')"
    ).fetchall()
    if len(rows) != 1 or rows[0]["kind"] not in {"open_pr", "push_update"}:
        return None
    rev = json.loads(rows[0]["payload_json"]).get("rev")
    return str(rows[0]["cid"]), str(rows[0]["id"]), rev if isinstance(rev, int) else None


async def relay(ctx: AppContext, handlers: Any, *, kind: ChatKind, topic: str | None, text: str, user_id: int,
                thread_id: int | None, message_id: int, stale_note: k.Line | None = None) -> None:  # fmt: skip
    """Route one message to Hermes (spawned by the handler, never inside the update slot)."""
    ui = ui_of(ctx)
    flat = ui.topics.mode == "flat"
    if ctx.chat is None:
        return
    turns = ctx.chat.service.turns
    if kind == "system_chat":
        assistant = ui.topics.thread_id("assistant")
        key = session_key(ui.topics, user_id, assistant, turns.session_key(user_id))
    else:
        key = session_key(ui.topics, user_id, thread_id, turns.session_key(user_id))
    if handlers.session_busy(key):
        ui_send.deliver(ctx, k.plain("fyi", k.line(STILL_ANSWERING)), thread_id=thread_id, reply_to=message_id)
        return
    handlers.spawn(_turn(ctx, kind=kind, topic=topic, text=text, key=key, thread_id=thread_id,
                         message_id=message_id, flat=flat, stale_note=stale_note),
                   "chat_turn", session_key=key)  # fmt: skip


async def _turn(ctx: AppContext, *, kind: ChatKind, topic: str | None, text: str, key: str, thread_id: int | None,
                message_id: int, flat: bool, stale_note: k.Line | None) -> None:  # fmt: skip
    assert ctx.chat is not None
    parts = [{"type": "input_text", "text": text[:USER_TEXT_MAX]}]
    try:
        outcome = await ctx.chat.service.ask(parts, key, user_message_id=message_id)
    except RelayClosed as closed:
        reply = closed.gate.reply or "🤖 Assistant unavailable."
        retry = k.button("🔁 Retry", "chat_retry", {"text": text[:USER_TEXT_MAX], "reply_to": message_id})
        ui_send.deliver(ctx, k.plain("fyi", k.line(reply), rows=[[retry]]), thread_id=thread_id,
                        reply_to=message_id)  # fmt: skip
        return
    if outcome.status != "done":
        retry = k.button("🔁 Retry", "chat_retry", {"text": text[:USER_TEXT_MAX], "reply_to": message_id})
        ui_send.deliver(ctx, k.plain("fyi", k.line("🤖 Assistant unavailable — try again."), rows=[[retry]]),
                        thread_id=thread_id, reply_to=message_id)  # fmt: skip
        return
    answer = outcome.answer or "(no answer)"
    if kind == "reviews_chat":
        _answer_in_reviews(ctx, answer, text, thread_id, message_id)
        return
    extra = stale_note
    if kind == "system_chat":
        origin = ORIGIN_NAMES.get(topic or "", "🚦 Control")
        extra = (k.Const(f'↪️ from {origin}: "'), k.Text(text, 80), k.Const('"'))
    for i, part in enumerate(chunks(answer)):
        first = extra if i == 0 else None
        blocks = quoted_answer(HEADER, part, extra=first) if flat else plain_answer(part, extra=first)
        view = k.View(kind="chat_answer", blocks=blocks)
        if kind == "system_chat":
            ui_send.deliver(ctx, view, thread="assistant")
        else:
            ui_send.deliver(ctx, view, thread_id=thread_id, reply_to=message_id if i == 0 else None)
    if kind == "system_chat":
        ui_send.deliver(ctx, k.plain("fyi", k.line(f"{icons.MODEL} Answered in 💬 Assistant")), thread_id=thread_id,
                        reply_to=message_id, silent=True)  # fmt: skip


def _answer_in_reviews(ctx: AppContext, answer: str, text: str, thread_id: int | None, message_id: int) -> None:
    parts = chunks(answer)  # cut at a line boundary within the message budget (rule 7)
    full_elsewhere = len(parts) > 1
    body = parts[0] + ("\n… full answer in 💬 Assistant" if full_elsewhere else "")
    rows: k.Rows = []
    target = _single_open_review_action(ctx)
    if target is not None:
        cid, action_id, rev = target
        rows = [[k.button("📝 Use as comment", "use_as_comment",
                          {"cid": cid, "rev": rev, "action_id": action_id, "message_id": message_id,
                           "text": cut(text, 4000)})]]  # fmt: skip
    view = k.View(kind="chat_answer", blocks=quoted_answer(HEADER, body), rows=rows)
    ui_send.deliver(ctx, view, thread_id=thread_id, reply_to=message_id)
    if full_elsewhere:
        for i, part in enumerate(chunks(answer)):
            extra = (k.Const("🤖 from your Reviews chat"),) if i == 0 else None
            ui_send.deliver(ctx, k.View(kind="chat_answer", blocks=plain_answer(part, extra=extra)), thread="assistant")
