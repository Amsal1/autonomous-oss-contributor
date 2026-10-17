"""The single gatekeeper-owned bot (DESIGN §12.8, §14.1, ADR-0004): sender, card delivery, polling.

* python-telegram-bot 22.x ``Application`` with long polling through proxy 3150
  (``HTTPXRequest(proxy=...)``), ``allowed_updates=["message", "callback_query"]``, pending
  updates processed at start (``drop_pending_updates=False``).
* Disabled (WARNING, doctor "awaiting secrets") without ``TELEGRAM_BOT_TOKEN`` or while
  ``profile.telegram.user_id == 0`` (PLAN Decision 5).
* Cards: message 1 is sent first; its message id binds every token (N12). Body parts follow; the
  Approve keyboard is minted and attached only when every part was delivered (§14.4).
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from datetime import timedelta
from typing import Any

from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import Application
from telegram.request import HTTPXRequest
from telegram.warnings import PTBDeprecationWarning

from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalError
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.outbox import OutboxRow, OutboxWorker, SendError
from ossbot.gatekeeper.telegram import cards, keyboards
from ossbot.gatekeeper.telegram.commands.registry import load_plugins, menu
from ossbot.gatekeeper.telegram.escape import esc
from ossbot.gatekeeper.telegram.handlers import Handlers
from telegram import Bot

log = logging.getLogger(__name__)
TELEGRAM_PROXY = "http://10.212.0.2:3150"
ALLOWED_UPDATES = ["message", "callback_query"]
OUTBOX_INTERVAL_SECONDS = 1.0  # <= 1 msg/s per chat
RETRY_TOKEN_TTL = timedelta(hours=24)
CARD_PREFIX = "card:"
RETRY_PREFIX = "retry:"


def telegram_enabled(ctx: AppContext, token: str | None) -> bool:
    return bool(token) and ctx.telegram_chat_id != 0


class TelegramSender:
    """``outbox.Sender`` over the Bot API; 429 -> ``retry_after``, 4xx -> permanent."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await self.bot.do_api_request(method, api_kwargs=payload)
        except RetryAfter as exc:
            with warnings.catch_warnings():  # PTB 22.2+: int today, timedelta in a future major
                warnings.simplefilter("ignore", PTBDeprecationWarning)
                delay = exc.retry_after
            seconds = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
            raise SendError(f"429 retry after {seconds}s", retry_after=seconds) from exc
        except BadRequest as exc:  # a NetworkError subclass in PTB, but never transient
            raise SendError(f"bad request: {exc.message}", permanent=True) from exc
        except (TimedOut, NetworkError) as exc:
            raise SendError(f"network: {exc.message}") from exc
        except TelegramError as exc:  # Forbidden, Conflict, ...
            raise SendError(f"telegram: {exc.message}", permanent=True) from exc
        return result if isinstance(result, dict) else {"result": result}


class CardPublisher:
    """Queues cards and retry notes through the outbox and finishes them as deliveries land."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def publish(self, approval_id: str) -> None:
        chat = self.ctx.telegram_chat_id
        if chat == 0:
            log.warning("telegram not configured; card %s not sent", approval_id, extra={"event": "card_dropped"})
            return
        rendered = cards.render(cards.view_for_approval(self.ctx.conn, approval_id))
        self.ctx.outbox.enqueue(
            "sendMessage",
            {"chat_id": chat, "text": rendered.card, "parse_mode": ParseMode.HTML, "disable_web_page_preview": True},
            dedupe_key=f"{CARD_PREFIX}{approval_id}",
            approval_id=approval_id,
        )

    def retry_note(self, action_id: str, text: str) -> None:
        chat = self.ctx.telegram_chat_id
        if chat == 0:
            self.ctx.notify(text)
            return
        self.ctx.outbox.enqueue(
            "sendMessage",
            {"chat_id": chat, "text": esc(text), "parse_mode": ParseMode.HTML},
            dedupe_key=f"{RETRY_PREFIX}{action_id}",
        )

    async def on_sent(self, row: OutboxRow, result: dict[str, Any]) -> None:
        key = row.dedupe_key or ""
        if key.startswith(CARD_PREFIX) and row.approval_id is not None:
            self._card_sent(row.approval_id, int(result["message_id"]))
        elif key.startswith(RETRY_PREFIX):
            self._retry_sent(key[len(RETRY_PREFIX) :], int(result["message_id"]))
        elif row.approval_id is not None and row.part_index is not None:
            self._maybe_arm(row.approval_id)

    def _card_sent(self, approval_id: str, message_id: int) -> None:
        self.ctx.approvals.set_card_message(approval_id, message_id)
        rendered = cards.render(cards.view_for_approval(self.ctx.conn, approval_id))
        for index, part in enumerate(rendered.parts):
            self.ctx.outbox.enqueue(
                "sendMessage",
                {"chat_id": self.ctx.telegram_chat_id, "text": part, "parse_mode": ParseMode.HTML},
                dedupe_key=f"part:{approval_id}:{index}",
                approval_id=approval_id,
                part_index=index,
            )

    def _maybe_arm(self, approval_id: str) -> None:
        row = db.one(self.ctx.conn, "SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if row is None or row["tg_message_id"] is None or row["status"] != "pending":
            return
        if row["body_parts_delivered"] < row["body_parts_total"]:
            return
        try:
            tokens = self.ctx.approvals.issue_card_tokens(approval_id)
        except ApprovalError as exc:
            log.warning("card tokens not issued: %s", exc.code, extra={"event": "card_tokens_refused"})
            return
        kind = cards.view_for_approval(self.ctx.conn, approval_id).kind
        self.ctx.outbox.enqueue(
            "editMessageReplyMarkup",
            {
                "chat_id": row["tg_chat_id"],
                "message_id": row["tg_message_id"],
                "reply_markup": keyboards.to_json(keyboards.card_rows(kind, tokens)),
            },
            dedupe_key=f"arm:{approval_id}",
        )

    def _retry_sent(self, action_id: str, message_id: int) -> None:
        chat = self.ctx.telegram_chat_id
        token = self.ctx.approvals.mint_token(
            "retry", chat_id=chat, message_id=message_id, expires_at=self.ctx.clock.now() + RETRY_TOKEN_TTL,
            arg=action_id,
        )  # fmt: skip
        self.ctx.outbox.enqueue(
            "editMessageReplyMarkup",
            {"chat_id": chat, "message_id": message_id,
             "reply_markup": keyboards.to_json(keyboards.single_row("🔁 Retry", token))},
            dedupe_key=f"retry-arm:{action_id}",
        )  # fmt: skip


def build_application(
    token: str, handlers: Handlers, proxy: str = TELEGRAM_PROXY
) -> Application[Any, Any, Any, Any, Any, Any]:
    application = (
        Application.builder()
        .token(token)
        .request(HTTPXRequest(proxy=proxy))
        .get_updates_request(HTTPXRequest(proxy=proxy, read_timeout=40.0))
        .build()
    )
    handlers.register(application)
    return application


class TelegramFrontend:
    """Start/stop the bot and the outbox loop inside the gatekeeper's event loop."""

    def __init__(self, ctx: AppContext, application: Application[Any, Any, Any, Any, Any, Any]) -> None:
        self.ctx = ctx
        self.application = application
        self.publisher = CardPublisher(ctx)
        self.worker = OutboxWorker(ctx.outbox, TelegramSender(application.bot), self.publisher.on_sent, batch=1)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        load_plugins()
        self.ctx.on_approval_created = self.publisher.publish
        await self.application.initialize()
        try:
            await self.application.bot.set_my_commands([(c["command"], c["description"]) for c in menu()])
        except TelegramError as exc:
            log.warning("setMyCommands failed: %s", exc, extra={"event": "telegram_menu_failed"})
        await self.application.start()
        assert self.application.updater is not None
        await self.application.updater.start_polling(allowed_updates=ALLOWED_UPDATES, drop_pending_updates=False)
        self._task = asyncio.create_task(self._outbox_loop())

    async def _outbox_loop(self) -> None:
        while True:
            try:
                await self.worker.run_once()
            except Exception:
                log.exception("outbox loop failed", extra={"event": "outbox_failed"})
            await asyncio.sleep(OUTBOX_INTERVAL_SECONDS)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self.application.updater is not None and self.application.updater.running:
            await self.application.updater.stop()
        if self.application.running:
            await self.application.stop()
        await self.application.shutdown()
