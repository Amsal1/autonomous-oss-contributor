"""Photos, albums, documents, voice and locations for the assistant (DESIGN §14.12.5, CR-002 Amendment A).

In the update slot (``accept``) only bounded local work happens: the message is classified from its
own fields, oversize or unsupported input is answered at once (with the 🤷 reaction), an accepted one
gets 👀, and the rest runs in a spawned ``media`` task:

* **single attachments**: the task downloads (``getFile`` through the bot's proxy client, capped twice),
  prepares (``ChatRuntime.prepare``: magic bytes, metadata stripping, the PDF sandbox job) and starts
  the turn (``chat_relay.start``);
* **albums**: messages with the same ``media_group_id`` are appended to one in-memory buffer; the first
  spawns the collector, which waits until ``album_window_s`` (1.5 s) passed since the last append,
  then downloads up to 10 images and starts one turn. Its ``on_done`` drops the buffer in every
  outcome, a cancel before it started included;
* **voice**: no transcription is available at the pinned versions (kiro-cli advertises no audio input,
  CR3-IH-10), so the reply is "Voice isn't supported yet — send text.";
* **location**: relayed as ``lat, lon`` text with a "📍 location shared" marker.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ossbot.common.config import AttachmentKind
from ossbot.gatekeeper.chat import media
from ossbot.gatekeeper.chat.code_jobs import attachment_note
from ossbot.gatekeeper.telegram import chat_relay
from ossbot.gatekeeper.telegram.routing import ChatKind
from ossbot.gatekeeper.telegram.runtime import ui_of
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

log = logging.getLogger(__name__)
ALBUM_MAX = 10
PHOTO_SIDE_MAX = 1600
ALBUM_POLL_S = 0.1
UNSUPPORTED = "I can read photos, PDFs and text files; this kind of message isn't supported."


@dataclass(frozen=True)
class Item:
    kind: AttachmentKind
    file_id: str
    size: int | None
    name: str
    as_file: bool = False


@dataclass
class Where:
    kind: ChatKind
    topic: str | None
    user_id: int
    thread_id: int | None
    stale_note: k.Line | None = None


@dataclass
class Album:
    where: Where
    first_message_id: int
    items: list[Item] = field(default_factory=list)
    captions: list[str] = field(default_factory=list)
    last_append: float = field(default_factory=time.monotonic)


def _photo(message: Any) -> Item:
    sizes = list(message.photo)
    fitting = [p for p in sizes if max(p.width, p.height) <= PHOTO_SIDE_MAX]
    best = max(fitting, key=lambda p: p.width * p.height) if fitting else min(sizes, key=lambda p: p.width * p.height)
    return Item("image", best.file_id, best.file_size, f"photo-{message.message_id}.jpg")


def classify(message: Any) -> Item | str:
    """The message's attachment, or the reply text that refuses it."""
    if message.photo:
        return _photo(message)
    if message.voice or message.audio:
        return media.VOICE_UNSUPPORTED
    doc = message.document
    if doc is not None:
        try:
            kind = media.classify(doc.mime_type, doc.file_name)
        except media.AttachmentRefused as exc:
            return exc.reply
        return Item(kind, doc.file_id, doc.file_size, doc.file_name or "file", as_file=kind == "image")
    return UNSUPPORTED


def location_text(message: Any) -> str | None:
    """A shared location as the turn's text (relayed like typed text)."""
    loc = message.location
    if loc is None:
        return None
    text = f"📍 location shared: {loc.latitude:.6f}, {loc.longitude:.6f}"
    return f"{text}\n{message.caption}" if message.caption else text


def _refuse(ctx: AppContext, reply: str, thread_id: int | None, message_id: int) -> None:
    ui_send.deliver(ctx, k.plain("fyi", k.line(reply)), thread_id=thread_id, reply_to=message_id)
    chat_relay.react(ctx, message_id, chat_relay.REACT_UNSUPPORTED)


def accept(ctx: AppContext, handlers: Any, message: Any, where: Where) -> None:
    """The update-slot part (no awaits): refuse, buffer an album message, or spawn the media task."""
    if ctx.chat is None:
        return
    caption = message.caption or ""
    item = classify(message)
    if isinstance(item, str):
        _refuse(ctx, item, where.thread_id, message.message_id)
        return
    try:
        media.check_size(item.size, _policy(ctx))
    except media.AttachmentRefused as exc:
        _refuse(ctx, exc.reply, where.thread_id, message.message_id)
        return
    group = message.media_group_id
    if group:
        album = handlers.albums.get(group)
        if album is None:
            album = Album(where, message.message_id)
            handlers.albums[group] = album
            chat_relay.react(ctx, message.message_id, chat_relay.REACT_SEEN)
            handlers.spawn(_collect(ctx, handlers, group, album), "media",
                           on_done=lambda _t: handlers.albums.pop(group, None))  # fmt: skip
        if len(album.items) < ALBUM_MAX:
            album.items.append(item)
        if caption:
            album.captions.append(caption)
        album.last_append = time.monotonic()
        return
    key, _ = chat_relay.session_for(ctx, where.kind, where.user_id, where.thread_id)
    if handlers.session_busy(key):
        chat_relay.busy_reply(ctx, where.thread_id, message.message_id)
        return
    chat_relay.react(ctx, message.message_id, chat_relay.REACT_SEEN)
    handlers.spawn(_single(ctx, handlers, where, message.message_id, item, caption), "media")


def _policy(ctx: AppContext) -> Any:
    policy = ctx.config.policy.chat.attachments
    tg_max = ctx.config.telegram.attachments.max_mb
    return policy.model_copy(update={"max_mb": min(policy.max_mb, tg_max)}) if tg_max < policy.max_mb else policy


async def _prepare(ctx: AppContext, handlers: Any, key: str, item: Item) -> tuple[list[dict[str, Any]], bool]:
    """Download and prepare one attachment -> (Hermes parts, is an image). Raises AttachmentRefused."""
    assert ctx.chat is not None
    policy = _policy(ctx)
    bot = handlers.application.bot
    data = await media.download(bot.get_file, item.file_id, item.size, policy)
    prepared = await ctx.chat.prepare(item.kind, data, item.name, policy, as_file=item.as_file)
    attachment_id = ctx.chat.media.put(key, item.name, prepared.data)
    return [*prepared.parts, {"type": "input_text", "text": attachment_note(attachment_id, item.name)}], (
        prepared.kind == "image"
    )


def _turn(ctx: AppContext, handlers: Any, where: Where, message_id: int, caption: str,
          parts: list[dict[str, Any]], vision: bool, count: int) -> None:  # fmt: skip
    key, answer_thread = chat_relay.session_for(ctx, where.kind, where.user_id, where.thread_id)
    if handlers.session_busy(key):
        chat_relay.busy_reply(ctx, where.thread_id, message_id)
        return
    text = caption or (f"(the user sent {count} images without a caption)" if count > 1
                       else "(the user sent an attachment without a caption)")  # fmt: skip
    spec = chat_relay.TurnSpec(
        where.kind, where.topic, caption, where.user_id, where.thread_id, message_id, key=key,
        answer_thread=answer_thread, flat=ui_of(ctx).topics.mode == "flat", stale_note=where.stale_note,
        parts=[{"type": "input_text", "text": text[: chat_relay.USER_TEXT_MAX]}, *parts], origin="media",
        had_attachments=True, vision=vision,
    )  # fmt: skip
    chat_relay.start(ctx, handlers, spec)


async def _single(ctx: AppContext, handlers: Any, where: Where, message_id: int, item: Item, caption: str) -> None:
    key, _ = chat_relay.session_for(ctx, where.kind, where.user_id, where.thread_id)
    try:
        parts, vision = await _prepare(ctx, handlers, key, item)
    except media.AttachmentRefused as exc:
        _refuse(ctx, exc.reply, where.thread_id, message_id)
        return
    _turn(ctx, handlers, where, message_id, caption, parts, vision, 1)


async def _collect(ctx: AppContext, handlers: Any, group: str, album: Album) -> None:
    window = ctx.config.telegram.attachments.album_window_s
    while time.monotonic() - album.last_append < window:
        await asyncio.sleep(ALBUM_POLL_S)
    handlers.albums.pop(group, None)  # later messages of the group start nothing (they are past the window)
    key, _ = chat_relay.session_for(ctx, album.where.kind, album.where.user_id, album.where.thread_id)
    parts: list[dict[str, Any]] = []
    vision = False
    for item in album.items[:ALBUM_MAX]:
        try:
            got, is_image = await _prepare(ctx, handlers, key, item)
        except media.AttachmentRefused as exc:
            _refuse(ctx, exc.reply, album.where.thread_id, album.first_message_id)
            return
        parts += got
        vision = vision or is_image
    _turn(
        ctx, handlers, album.where, album.first_message_id, "\n".join(album.captions), parts, vision, len(album.items)
    )
