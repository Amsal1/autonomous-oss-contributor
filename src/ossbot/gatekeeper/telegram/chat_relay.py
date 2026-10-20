"""Routing rules 7-9 (DESIGN §14.10.4) and the streamed assistant turn (§14.12.1, U6, U12, U14).

* ✅ Reviews (rule 7): session ``tg-<uid>-reviews``; classic, under the gatekeeper header ``🤖 Assistant ·
  answer to your message`` with the whole body in one quote (cut at a part boundary with a pointer; the
  full answer goes to 💬 Assistant). ``[📝 Use as comment]`` when exactly one ``open_pr``/``push_update``
  approval is open.
* 🚦 Control / 📈 OSS feed (rule 8): answered in 💬 Assistant with ``↪️ from …``; the origin gets one
  silent ``🤖 Answered in 💬 Assistant`` reply.
* Everything else (rule 9): the topic's own session. In flat mode every answer and every draft carries
  the header and the quote (AC 48).

``relay`` runs in the update slot and does only bounded local work (§14.12.1 step 0): the session-map
check, the ``chat_turns`` row, the 👀 reaction (outbox) and ``Handlers.spawn(..., "chat_turn",
session_key=..., on_done=finalize_turn(...))``; there is no ``await`` between the check and the spawn.
The turn task streams drafts (``chat/drafts.py``), ends on completion, the 200 KB cap or a Stop
(``Handlers.stop_turn`` sets the turn's stop event, §14.12.1 step 5), and sends the final answer
through the outbox (``chat/answer_view.py``). A stream that did not complete normally is shown and
stored as ``draft_visible(text) + "…"`` (U14).
"""

from __future__ import annotations

import asyncio
import json
import logging
import warnings
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from ossbot.common.models import requested
from ossbot.gatekeeper.chat import answer_view, drafts, followups
from ossbot.gatekeeper.chat import stream as chat_stream
from ossbot.gatekeeper.chat.backend import RelayClosed
from ossbot.gatekeeper.chat.hermes_api import HermesError, HermesUnavailable
from ossbot.gatekeeper.chat.turns import Origin, finalize_turn
from ossbot.gatekeeper.telegram.routing import ChatKind
from ossbot.gatekeeper.telegram.runtime import ui_of
from ossbot.gatekeeper.telegram.topics import session_key
from ossbot.gatekeeper.telegram.ui import icons, rich
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send
from ossbot.gatekeeper.telegram.ui.limits import cut
from ossbot.gatekeeper.telegram.ui.redact import draft_visible, redact

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

log = logging.getLogger(__name__)
STILL_ANSWERING = "⏳ Still answering — tap ⏹ Stop or wait"
HEADER = answer_view.HEADER
ORIGIN_NAMES = {"control": "🚦 Control", "feed": "📈 OSS feed"}
USER_TEXT_MAX = 4000
FROM_REVIEWS = "🤖 from your Reviews chat"
FULL_IN_ASSISTANT = "… full answer in 💬 Assistant"
UNAVAILABLE = "🤖 Assistant unavailable — try again."
REACT_SEEN, REACT_DONE, REACT_UNSUPPORTED = "👀", "👌", "🤷"


@dataclass
class TurnSpec:
    kind: ChatKind
    topic: str | None
    text: str  # the user's text (or the follow-up label); never shown back except the follow-up echo
    user_id: int
    thread_id: int | None  # the originating thread
    message_id: int  # the user's message (reactions, reply_to); a follow-up tap: the answer message
    key: str = ""
    answer_thread: int | None = None  # where drafts and the answer go
    flat: bool = False
    stale_note: k.Line | None = None
    parts: list[dict[str, Any]] = field(default_factory=list)
    origin: Origin = "typed"
    had_attachments: bool = False
    vision: bool = False
    react: bool = True
    prefix: list[k.Line] = field(default_factory=list)  # gatekeeper lines above the answer (🎙 You said …)


def react(ctx: AppContext, message_id: int, emoji: str) -> None:
    """``setMessageReaction`` through the outbox (§14.12.5 Acknowledge)."""
    if ctx.telegram_chat_id == 0 or message_id <= 0:
        return
    payload = {"chat_id": ctx.telegram_chat_id, "message_id": message_id,
               "reaction": [{"type": "emoji", "emoji": emoji}]}  # fmt: skip
    ctx.outbox.enqueue("setMessageReaction", payload, dedupe_key=f"react:{message_id}:{emoji}")


def session_for(ctx: AppContext, kind: ChatKind, user_id: int, thread_id: int | None) -> tuple[str, int | None]:
    ui = ui_of(ctx)
    assert ctx.chat is not None
    flat_key = ctx.chat.service.turns.session_key(user_id)
    if kind == "system_chat":
        assistant = ui.topics.thread_id("assistant")
        return session_key(ui.topics, user_id, assistant, flat_key), assistant
    return session_key(ui.topics, user_id, thread_id, flat_key), thread_id


def busy_reply(ctx: AppContext, thread_id: int | None, message_id: int) -> None:
    ui_send.deliver(ctx, k.plain("fyi", k.line(STILL_ANSWERING)), thread_id=thread_id, reply_to=message_id)


async def relay(ctx: AppContext, handlers: Any, *, kind: ChatKind, topic: str | None, text: str, user_id: int,
                thread_id: int | None, message_id: int, stale_note: k.Line | None = None,
                parts: list[dict[str, Any]] | None = None, origin: Origin = "typed", had_attachments: bool = False,
                vision: bool = False, prefix: list[k.Line] | None = None) -> bool:  # fmt: skip
    """Start one turn (in the update slot: no awaits). Returns False when nothing was started."""
    if ctx.chat is None:
        return False
    key, answer_thread = session_for(ctx, kind, user_id, thread_id)
    if handlers.session_busy(key):
        busy_reply(ctx, thread_id, message_id)
        return False
    spec = TurnSpec(kind, topic, text, user_id, thread_id, message_id, key=key, answer_thread=answer_thread,
                    flat=ui_of(ctx).topics.mode == "flat", stale_note=stale_note,
                    parts=parts if parts is not None else [{"type": "input_text", "text": text[:USER_TEXT_MAX]}],
                    origin=origin, had_attachments=had_attachments, vision=vision, react=origin != "followup_tap",
                    prefix=list(prefix or []))  # fmt: skip
    start(ctx, handlers, spec)
    return True


def start(ctx: AppContext, handlers: Any, spec: TurnSpec) -> int:
    """The row, the 👀 reaction and the spawn (§14.12.1 step 0). The caller checked the session is free."""
    assert ctx.chat is not None
    turns = ctx.chat.service.turns
    turn_id = turns.start(spec.key, spec.message_id, thread_id=spec.answer_thread, origin=spec.origin,
                          had_attachments=spec.had_attachments)  # fmt: skip
    if spec.kind == "reviews_chat":
        turns.set_user_text(turn_id, spec.text)
    if spec.react:
        react(ctx, spec.message_id, REACT_SEEN)
    stop = handlers.stop_event(turn_id)
    on_done = finalize_turn(turns, turn_id)

    def done(task: asyncio.Task[Any]) -> None:
        handlers.forget_stop(turn_id)
        on_done(task)

    handlers.spawn(run_turn(ctx, handlers, spec, turn_id, stop), "chat_turn", session_key=spec.key, on_done=done)
    return turn_id


# ------------------------------------------------------------------------------- the turn task
def _retry(spec: TurnSpec) -> k.ButtonSpec:
    return k.button("🔁 Retry", "chat_retry", {"text": spec.text[:USER_TEXT_MAX], "reply_to": spec.message_id})


def _closed(ctx: AppContext, spec: TurnSpec, reply: str) -> None:
    ui_send.deliver(ctx, k.plain("fyi", k.line(reply), rows=[[_retry(spec)]]), thread_id=spec.thread_id,
                    reply_to=spec.message_id)  # fmt: skip


def _models(ctx: AppContext, vision: bool) -> tuple[str, str | None]:
    """(``roles.chat`` model id, the vision model when the turn had images)."""
    try:
        cfg = ctx.models.read()
        return requested(cfg, "chat")[0], requested(cfg, "vision")[0] if vision else None
    except (OSError, ValueError, AttributeError):
        return "", None


def _heartbeat(state: chat_stream.StreamState) -> str:
    line = f"⏳ {state.elapsed_s} s"
    if state.fetches:
        line += f" · reading {state.fetches} page{'s' if state.fetches != 1 else ''}"
    return line


async def direct(handlers: Any, method: str, payload: dict[str, Any]) -> Any:
    """A Bot API call outside the outbox (drafts and typing only: never persisted, §14.12.1 step 3)."""
    bot = handlers.application.bot
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return await bot.do_api_request(method, api_kwargs=payload)


async def run_turn(ctx: AppContext, handlers: Any, spec: TurnSpec, turn_id: int, stop: asyncio.Event) -> None:
    assert ctx.chat is not None
    backend, turns = ctx.chat.service.backend, ctx.chat.service.turns
    gate = await backend.gate()
    if not gate.open:
        turns.finalize(turn_id, cancelled=False, failed=True)
        _closed(ctx, spec, gate.reply or "🤖 Assistant unavailable.")
        return
    ui = ui_of(ctx)
    wrapped = spec.flat or spec.kind == "reviews_chat"
    rich_ok = spec.kind != "reviews_chat" and rich.use_rich(k.View("chat_answer", [], rich=True), ui.prefs,
                                                            ctx.config.telegram.rich_views_default)  # fmt: skip
    state = chat_stream.StreamState()
    cfg = ctx.config.telegram.drafts

    def build(heartbeat: bool) -> drafts.DraftContent:
        return drafts.content(state.text, status=state.status_line(), heartbeat=_heartbeat(state) if heartbeat
                              else None, wrapped=wrapped, rich=rich_ok, header=HEADER)  # fmt: skip

    lane = drafts.DraftLane(
        lambda method, payload: direct(handlers, method, payload), build, chat_id=ctx.telegram_chat_id,
        thread_id=spec.answer_thread, draft_id=turn_id, rich=rich_ok, outbox_due=lambda: bool(ctx.outbox.due(1)),
        min_interval_s=cfg.min_interval_s, heartbeat_s=cfg.heartbeat_s,
        on_rich_refused=lambda: rich.mark_unavailable(ui.prefs),
    )  # fmt: skip
    await lane.send_once()  # step 2: the "Thinking…" draft at once (the draft a Stop targets)
    lane_task = asyncio.ensure_future(lane.run())
    try:
        events = backend.stream(spec.parts, spec.key)
        end = await chat_stream.consume(events, state, stop, lane.touch)
    except (HermesError, HermesUnavailable, RelayClosed) as exc:
        turns.finalize(turn_id, cancelled=False, failed=True)
        log.warning("chat turn failed: %s", type(exc).__name__, extra={"event": "chat_turn_failed"})
        reply = exc.gate.reply if isinstance(exc, RelayClosed) and exc.gate.reply else UNAVAILABLE
        _closed(ctx, spec, reply)
        return
    finally:
        lane.close()
        lane_task.cancel()
        await asyncio.gather(lane_task, return_exceptions=True)
    finish(ctx, spec, turn_id, state, end)


def finish(ctx: AppContext, spec: TurnSpec, turn_id: int, state: chat_stream.StreamState,
           end: chat_stream.End) -> None:  # fmt: skip
    """Redact, store and send the final answer (§14.12.1 step 4; Stop: step 5)."""
    assert ctx.chat is not None
    body, labels = followups.extract(state.text)
    if end == "completed":
        shown = redact(body, surface="chat_answer")[0]
    else:
        shown = draft_visible(body).rstrip() + "…"
        labels = []
    chat_model, vision_model = _models(ctx, spec.vision)
    model = state.model or chat_model
    stored = ctx.chat.service.turns.done(turn_id, shown, model=model or None, tools=state.tools,
                                         run_id=state.response_id or None,
                                         status="stopped" if end == "stopped" else "done")  # fmt: skip
    answer = answer_view.Answer(
        turn_id, stored or "(no answer)", end, sources=state.sources(stored), followups=labels, model=model,
        fallback=bool(state.model and chat_model and state.model != chat_model), vision_model=vision_model,
        searches=state.searches, fetches=state.fetches, elapsed_s=state.elapsed_s,
    )  # fmt: skip
    if spec.kind == "reviews_chat":
        _answer_in_reviews(ctx, spec, answer)
    else:
        _answer(ctx, spec, answer)
    if spec.react:
        react(ctx, spec.message_id, REACT_DONE)


def _extra(spec: TurnSpec) -> k.Line | None:
    if spec.kind == "system_chat":
        origin = ORIGIN_NAMES.get(spec.topic or "", "🚦 Control")
        return (k.Const(f'↪️ from {origin}: "'), k.Text(spec.text, 80), k.Const('"'))
    return spec.stale_note


def _deliver_all(ctx: AppContext, spec: TurnSpec, views: list[k.View], *, to_assistant: bool) -> None:
    for i, view in enumerate(views):
        if to_assistant:
            ui_send.deliver(ctx, view, thread="assistant")
        else:
            ui_send.deliver(ctx, view, thread_id=spec.thread_id, reply_to=spec.message_id if i == 0 else None)


def _prefix_view(ctx: AppContext, spec: TurnSpec, *, to_assistant: bool) -> None:
    if spec.prefix:
        view = k.View(kind="chat_answer", blocks=[k.Block((k.Quote(tuple(spec.prefix), expandable=True),))])
        _deliver_all(ctx, spec, [view], to_assistant=to_assistant)


def _answer(ctx: AppContext, spec: TurnSpec, answer: answer_view.Answer) -> None:
    ui = ui_of(ctx)
    rich_ok = rich.use_rich(k.View("chat_answer", [], rich=True), ui.prefs, ctx.config.telegram.rich_views_default)
    to_assistant = spec.kind == "system_chat"
    _prefix_view(ctx, spec, to_assistant=to_assistant)
    views = answer_view.views(answer, wrapped=spec.flat, rich=rich_ok, extra=_extra(spec))
    _deliver_all(ctx, spec, views, to_assistant=to_assistant)
    if answer_view.needs_document(answer, rich=rich_ok, wrapped=spec.flat):
        ui_send.document(ctx, "answer.md", answer.text.encode("utf-8"), thread="assistant" if to_assistant else None,
                         thread_id=None if to_assistant else spec.thread_id)  # fmt: skip
    if to_assistant:
        ui_send.deliver(ctx, k.plain("fyi", k.line(f"{icons.MODEL} Answered in 💬 Assistant")),
                        thread_id=spec.thread_id, reply_to=spec.message_id, silent=True)  # fmt: skip


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


def _answer_in_reviews(ctx: AppContext, spec: TurnSpec, answer: answer_view.Answer) -> None:
    """Rule 7: classic, header + quote; a long answer keeps its first part here with a pointer."""
    parts = answer_view.split(answer.text, rich=False, quoted=True)
    full_elsewhere = len(parts) > 1
    rows: k.Rows = []
    target = _single_open_review_action(ctx)
    if target is not None:
        cid, action_id, rev = target
        rows = [[k.button("📝 Use as comment", "use_as_comment",
                          {"cid": cid, "rev": rev, "action_id": action_id, "message_id": spec.message_id,
                           "text": cut(spec.text, 4000)})]]  # fmt: skip
    here = answer
    if full_elsewhere:
        here = replace(answer, text=f"{parts[0]}\n\n{FULL_IN_ASSISTANT}", followups=[])
    _prefix_view(ctx, spec, to_assistant=False)
    first = answer_view.views(here, wrapped=True, rich=False, rows=rows)[0]
    ui_send.deliver(ctx, first, thread_id=spec.thread_id, reply_to=spec.message_id)
    if full_elsewhere:
        ui = ui_of(ctx)
        rich_ok = rich.use_rich(k.View("chat_answer", [], rich=True), ui.prefs,
                                ctx.config.telegram.rich_views_default)  # fmt: skip
        views = answer_view.views(answer, wrapped=spec.flat, rich=rich_ok, extra=(k.Const(FROM_REVIEWS),))
        _deliver_all(ctx, spec, views, to_assistant=True)
