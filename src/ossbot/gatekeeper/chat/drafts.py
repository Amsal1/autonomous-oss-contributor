"""The draft lane (DESIGN §14.12.1 steps 2-3 and 7): in memory, never a ``tg_outbox`` row.

One ``DraftLane`` per streaming turn. ``touch`` marks the turn's content changed; ``run`` (a child task
of the turn) sends the newest content as ``sendRichMessageDraft`` (or ``sendMessageDraft`` when rich
is unavailable), both with ``draft_id = turn_id`` and ``can_stop: true``:

* coalesced (the latest state wins) and at most one draft per ``min_interval_s`` per chat, across every
  lane of that chat;
* it yields while the outbox has a due row (cards and alerts first);
* a heartbeat at least every ``heartbeat_s`` (a draft lives 30 s), with "⏳ 45 s · reading 2 pages";
* a rich draft that Telegram refuses switches the lane to classic drafts; a refused classic draft
  switches it to ``sendChatAction("typing")`` every 4.5 s.

The content builder is the turn's: it already applied the U14 hold-back (``draft_visible``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from ossbot.gatekeeper.chat import md2blocks
from ossbot.gatekeeper.telegram import rawapi
from ossbot.gatekeeper.telegram.escape import defuse_commands, esc, make_visible
from ossbot.gatekeeper.telegram.ui import blocks as rb
from ossbot.gatekeeper.telegram.ui.limits import DRAFT_TAIL_MAX
from ossbot.gatekeeper.telegram.ui.redact import draft_visible, redact

log = logging.getLogger(__name__)
TICK_S = 0.2
TYPING_EVERY_S = 4.5
DRAFT_TEXT_MAX = 4096


@dataclass(frozen=True)
class DraftContent:
    rich: rb.InputRichMessage | None  # None: classic only
    html: str  # classic draft text (parse mode HTML), <= 4,096


RICH_DRAFT_MAX = 24_000  # characters of the visible tail in a rich draft (budget 30,000)
THINKING = "Thinking…"


def content(raw: str, *, status: str, heartbeat: str | None, wrapped: bool, rich: bool,
            header: str) -> DraftContent:  # fmt: skip
    """One draft from the whole accumulated text, in the U14 order: ``draft_visible`` (redact the full
    text, then hold back the trailing incomplete token), then ``md2blocks``. The status and heartbeat
    lines are redacted too. ``wrapped`` (flat mode, Reviews): header + one quote, like the answer (AC 48)."""
    visible = draft_visible(raw)
    extra = [redact(s, surface="draft_status")[0] for s in (status, heartbeat) if s]
    html_body = esc(defuse_commands(make_visible(visible[-DRAFT_TAIL_MAX:])[0]))
    if wrapped:
        html = f"{esc(header)}\n<blockquote expandable>{html_body or esc(THINKING)}</blockquote>"
    else:
        html = md2blocks.classic_html(visible[-DRAFT_TAIL_MAX:]) if visible else ""
    if extra:
        html = f"{html}\n<i>{esc(' · '.join(extra))}</i>" if html else f"<i>{esc(' · '.join(extra))}</i>"
    message: rb.InputRichMessage | None = None
    if rich:
        tail = visible[-RICH_DRAFT_MAX:]
        if wrapped:
            paras = [rb.Paragraph(text=make_visible(p)[0]) for p in tail.split("\n") if p.strip()]
            body: list[rb.Block] = [rb.Paragraph(text=header)]
            body.append(rb.BlockQuote(blocks=paras) if paras else rb.Thinking(text=THINKING))
        else:
            body = md2blocks.rich_blocks(tail) or [rb.Thinking(text=THINKING)]
        body += [rb.Paragraph(text=line) for line in extra]
        message = rb.InputRichMessage(blocks=body)
        try:
            rb.check_budget(message)
        except rb.RichBudgetError:
            message = None  # this draft goes out classic
    if len(html) > DRAFT_TEXT_MAX:  # heavy escaping: a shorter plain tail, never a cut inside a tag
        html = esc(defuse_commands(make_visible(visible[-800:])[0]))
    return DraftContent(message, html)


Send = Callable[[str, dict[str, Any]], Awaitable[Any]]
Build = Callable[[bool], DraftContent]  # True: a heartbeat (no new content since the last draft)


class DraftLane:
    _last_by_chat: ClassVar[dict[int, float]] = {}

    def __init__(self, send: Send, build: Build, *, chat_id: int, thread_id: int | None, draft_id: int, rich: bool,
                 outbox_due: Callable[[], bool], min_interval_s: float = 1.0, heartbeat_s: float = 20.0,
                 monotonic: Callable[[], float] = time.monotonic,
                 on_rich_refused: Callable[[], None] | None = None) -> None:  # fmt: skip
        self._send, self._build = send, build
        self.chat_id, self.thread_id, self.draft_id = chat_id, thread_id, draft_id
        self.mode: str = "rich" if rich else "classic"  # rich | classic | typing
        self._outbox_due, self._min, self._beat, self._mono = outbox_due, min_interval_s, heartbeat_s, monotonic
        self._on_rich_refused = on_rich_refused
        self._dirty = True
        self._closed = False
        self._last_sent = 0.0
        self._last_typing = 0.0
        self.sent = 0

    def touch(self) -> None:
        self._dirty = True

    def close(self) -> None:
        self._closed = True

    def _due(self, now: float) -> bool:
        if now - self._last_by_chat.get(self.chat_id, 0.0) < self._min:
            return False
        return self._dirty or now - self._last_sent >= self._beat

    async def run(self) -> None:
        while not self._closed:
            now = self._mono()
            if self.mode == "typing":
                if now - self._last_typing >= TYPING_EVERY_S:
                    self._last_typing = now
                    await self._call("sendChatAction", self._typing_payload())
            elif self._due(now) and not self._outbox_due():
                heartbeat = not self._dirty
                self._dirty = False
                await self.send_once(heartbeat)
            await asyncio.sleep(TICK_S)

    def _typing_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": self.chat_id, "action": "typing"}
        if self.thread_id is not None:
            payload["message_thread_id"] = self.thread_id
        return payload

    async def send_once(self, heartbeat: bool = False) -> None:
        self._dirty = False
        content = self._build(heartbeat)
        now = self._mono()
        self._last_sent = now
        self._last_by_chat[self.chat_id] = now
        if self.mode == "rich" and content.rich is not None:
            req = rawapi.SendRichMessageDraft(
                chat_id=self.chat_id, message_thread_id=self.thread_id, draft_id=self.draft_id,
                rich_message=content.rich, can_stop=True, keep_on_stop=False,
            )  # fmt: skip
            if await self._call("sendRichMessageDraft", req.payload()):
                self.sent += 1
                return
            self.mode = "classic"
            if self._on_rich_refused is not None:
                self._on_rich_refused()
        if self.mode != "typing":
            classic = rawapi.SendMessageDraft(
                chat_id=self.chat_id, message_thread_id=self.thread_id, draft_id=self.draft_id,
                text=content.html[:DRAFT_TEXT_MAX] or None, parse_mode="HTML" if content.html else None,
                can_stop=True, keep_on_stop=False,
            )  # fmt: skip
            if await self._call("sendMessageDraft", classic.payload()):
                self.sent += 1
                return
            self.mode = "typing"

    async def _call(self, method: str, payload: dict[str, Any]) -> bool:
        try:
            await self._send(method, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a refused or failed draft never fails the turn
            log.warning("%s failed: %s", method, type(exc).__name__, extra={"event": "draft_failed", "method": method})
            return False
        return True
