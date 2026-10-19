"""The single gatekeeper-owned bot (DESIGN §12.8, §14.1, ADR-0004, ADR-0020): sender, card delivery,
the workspace (topics, menus, dashboard; §14.10) and polling.

* python-telegram-bot 22.8 ``Application`` with long polling through proxy 3150
  (``HTTPXRequest(proxy=...)``), ``allowed_updates=["message", "callback_query"]``, pending
  updates processed at start (``drop_pending_updates=False``). Bot API 10.1-10.3 payloads go through
  ``do_api_request`` (``telegram/rawapi.py``).
* Disabled (WARNING, doctor "awaiting secrets") without ``TELEGRAM_BOT_TOKEN`` or while
  ``profile.telegram.user_id == 0`` (PLAN Decision 5).
* Cards: message 1 is sent first; its message id binds every token (N12). Body parts follow; the
  Approve keyboard is minted and attached only when every part was delivered (§14.11.1, §14.18.4.4).
  Cards go to ✅ Reviews.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import warnings
from datetime import timedelta
from pathlib import Path
from typing import Any

from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import Application
from telegram.request import BaseRequest, HTTPXRequest, RequestData
from telegram.warnings import PTBDeprecationWarning, PTBUserWarning

from ossbot.common.clock import to_iso
from ossbot.common.ids import new_token
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalError
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.outbox import OutboxRow, OutboxWorker, SendError
from ossbot.gatekeeper.pollstats import PollStats
from ossbot.gatekeeper.telegram import keyboards
from ossbot.gatekeeper.telegram.commands.registry import load_plugins
from ossbot.gatekeeper.telegram.escape import esc
from ossbot.gatekeeper.telegram.handlers import Handlers
from ossbot.gatekeeper.telegram.runtime import ui_of
from ossbot.gatekeeper.telegram.scopes import ONLINE, offline_description
from ossbot.gatekeeper.telegram.topics import THREAD_NOT_FOUND
from ossbot.gatekeeper.telegram.ui import rich as ui_rich
from telegram import Bot, InputFile

log = logging.getLogger(__name__)
TELEGRAM_PROXY = "http://10.212.0.2:3150"
ALLOWED_UPDATES = ["message", "callback_query"]
OUTBOX_INTERVAL_SECONDS = 1.0  # <= 1 msg/s per chat
RETRY_TOKEN_TTL = timedelta(hours=24)
CONNECT_BACKOFF_MIN_S = 2.0
CONNECT_BACKOFF_MAX_S = 60.0
CARD_PREFIX = "card:"
RETRY_PREFIX = "retry:"
# The three card families (§14.18.4.4): approval id prefix -> table.
DECISION_TABLES: dict[str, str] = {"a_": "approvals", "p_": "proposal_approvals", "s_": "skill_approvals"}


def telegram_enabled(ctx: AppContext, token: str | None) -> bool:
    return bool(token) and ctx.telegram_chat_id != 0


class TelegramSender:
    """``outbox.Sender`` over the Bot API; 429 -> ``retry_after``, 4xx -> permanent."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Keys starting with ``_`` are ossbot's own (the classic fallback of a rich view, a spooled
        document's path) and never reach Telegram."""
        api_kwargs: dict[str, Any] = {key: value for key, value in payload.items() if not key.startswith("_")}
        doc_path = payload.get("_document_path")
        if method == "sendDocument" and isinstance(doc_path, str):
            path = Path(doc_path)
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise SendError(f"document spool: {type(exc).__name__}", permanent=True) from exc
            api_kwargs["document"] = InputFile(data, filename=str(payload.get("_document_name") or path.name))
        result = await self._call(method, api_kwargs)
        if method == "sendDocument" and isinstance(doc_path, str):
            Path(doc_path).unlink(missing_ok=True)
        return result

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        """A direct call outside the outbox (getMe, topics, scopes); the result object."""
        return await self._call(method, payload)

    async def _call(self, method: str, api_kwargs: dict[str, Any]) -> dict[str, Any]:
        try:
            with warnings.catch_warnings():  # raw calls are deliberate (ADR-0020): one code path for every method
                warnings.filterwarnings("ignore", message="Please use 'Bot\\.", category=PTBUserWarning)
                result = await self.bot.do_api_request(method, api_kwargs=api_kwargs)
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
        """Message 1 of an ``a_``, ``p_`` or ``s_`` card (resolved through ``ctx.decisions.for_id``)."""
        chat = self.ctx.telegram_chat_id
        if chat == 0:
            log.warning("telegram not configured; card %s not sent", approval_id, extra={"event": "card_dropped"})
            return
        rendered = self.ctx.decisions.for_id(approval_id).card_view(approval_id)
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
        """Message id + every part enqueue in one transaction (delta 8); a card without parts
        (proposals) is armed right away."""
        svc = self.ctx.decisions.for_id(approval_id)
        with db.transaction(self.ctx.conn):
            svc.set_card_message(approval_id, message_id)
            rendered = svc.card_view(approval_id)
            for index, part in enumerate(rendered.parts):
                self.ctx.outbox.enqueue(
                    "sendMessage",
                    {"chat_id": self.ctx.telegram_chat_id, "text": part, "parse_mode": ParseMode.HTML},
                    dedupe_key=f"part:{approval_id}:{index}",
                    approval_id=approval_id,
                    part_index=index,
                )
        if svc.part_count(approval_id) == 0:
            self._maybe_arm(approval_id)

    def _decision_row(self, approval_id: str) -> Any:
        table = DECISION_TABLES.get(approval_id[:2])
        if table is None:
            return None
        return db.one(self.ctx.conn, f"SELECT * FROM {table} WHERE id = ?", (approval_id,))  # noqa: S608

    def _maybe_arm(self, approval_id: str) -> None:
        """Card tokens + the ``arm:<aid>`` edit in one transaction (delta 8): "no arm row" means "no
        card tokens", so Approve is minted exactly once."""
        svc = self.ctx.decisions.for_id(approval_id)
        row = self._decision_row(approval_id)
        if row is None or row["tg_message_id"] is None or row["status"] != "pending":
            return
        if svc.part_count(approval_id) and row["body_parts_delivered"] < row["body_parts_total"]:
            return
        try:
            with db.transaction(self.ctx.conn):
                tokens = svc.issue_card_tokens(approval_id)
                self.ctx.outbox.enqueue(
                    "editMessageReplyMarkup",
                    {
                        "chat_id": row["tg_chat_id"],
                        "message_id": row["tg_message_id"],
                        "reply_markup": keyboards.to_json(svc.card_rows(approval_id, tokens)),
                    },
                    dedupe_key=f"arm:{approval_id}",
                )
        except ApprovalError as exc:
            log.warning("card tokens not issued: %s", exc.code, extra={"event": "card_tokens_refused"})

    def render_s1(self, approval_id: str, armed_at: str, tokens: dict[str, str]) -> None:
        """Delta 2: a timed-out Confirm goes back to the S1 card with fresh tokens. Called inside the
        service's disarm transaction; the key carries ``armed_at`` (it can repeat per approval, BA-3)."""
        svc = self.ctx.decisions.for_id(approval_id)
        row = self._decision_row(approval_id)
        if row is None or row["tg_message_id"] is None:
            return
        self.ctx.outbox.enqueue(
            "editMessageText",
            {"chat_id": row["tg_chat_id"], "message_id": row["tg_message_id"],
             "text": svc.card_view(approval_id).card, "parse_mode": ParseMode.HTML,
             "disable_web_page_preview": True,
             "reply_markup": keyboards.to_json(svc.card_rows(approval_id, tokens))},
            dedupe_key=f"disarm:{approval_id}:{armed_at}",
        )  # fmt: skip

    def recover(self) -> None:
        """Delta 8 (§14.18.4.4): heal every crash window of card delivery. Run once before polling
        starts and on every housekeeping tick (``ctx.card_recover``). Never counts parts."""
        conn = self.ctx.conn
        for table in DECISION_TABLES.values():
            # 0. approval created, card never enqueued (a failed on_approval_created after a commit)
            for r in conn.execute(
                f"SELECT d.id FROM {table} d WHERE d.status = 'pending' AND d.tg_message_id IS NULL"  # noqa: S608
                " AND NOT EXISTS (SELECT 1 FROM tg_outbox o WHERE o.dedupe_key = 'card:' || d.id)"
            ).fetchall():
                self.ctx.on_approval_created(str(r["id"]))
            # 1. card sent, message id lost
            for r in conn.execute(
                f"SELECT d.id, o.result_message_id FROM {table} d JOIN tg_outbox o ON o.dedupe_key = 'card:' || d.id"  # noqa: S608
                " WHERE d.status = 'pending' AND d.tg_message_id IS NULL AND o.status = 'sent'"
            ).fetchall():
                if r["result_message_id"] is None:
                    log.warning("card %s sent before result_message_id existed; reach it through /queue", r["id"],
                                extra={"event": "card_recover_skipped"})  # fmt: skip
                    continue
                self._card_sent(str(r["id"]), int(r["result_message_id"]))
            # 2. parts delivered (or none), never armed
            parts = "" if table == "proposal_approvals" else " AND d.body_parts_delivered >= d.body_parts_total"
            for r in conn.execute(
                f"SELECT d.id FROM {table} d WHERE d.status = 'pending' AND d.tg_message_id IS NOT NULL{parts}"  # noqa: S608
                " AND NOT EXISTS (SELECT 1 FROM tg_outbox o WHERE o.dedupe_key = 'arm:' || d.id)"
            ).fetchall():
                self._maybe_arm(str(r["id"]))

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


class PollTrackingRequest(BaseRequest):
    """Wraps the ``getUpdates`` request to record long-poll health (CR-001 item 1).

    PTB's polling loop already retries network errors with backoff and only advances the offset
    after a successful ``getUpdates``, so pending updates survive IP changes, sleep and outages;
    this wrapper counts failures and reconnects for ``/status`` and ``status.ps1``.
    """

    def __init__(self, inner: BaseRequest, stats: PollStats) -> None:
        self.inner = inner
        self.stats = stats

    @property
    def read_timeout(self) -> float | None:
        return self.inner.read_timeout

    async def initialize(self) -> None:
        await self.inner.initialize()

    async def shutdown(self) -> None:
        await self.inner.shutdown()

    async def do_request(
        self,
        url: str,
        method: str,
        request_data: RequestData | None = None,
        read_timeout: Any = BaseRequest.DEFAULT_NONE,
        write_timeout: Any = BaseRequest.DEFAULT_NONE,
        connect_timeout: Any = BaseRequest.DEFAULT_NONE,
        pool_timeout: Any = BaseRequest.DEFAULT_NONE,
    ) -> tuple[int, bytes]:
        try:
            code, body = await self.inner.do_request(
                url, method, request_data, read_timeout=read_timeout, write_timeout=write_timeout,
                connect_timeout=connect_timeout, pool_timeout=pool_timeout,
            )  # fmt: skip
        except TelegramError as exc:  # NetworkError / TimedOut: PTB retries with backoff
            self.stats.failed(f"{type(exc).__name__}: {exc.message}")
            raise
        if url.endswith("/getUpdates"):
            if 200 <= code < 300:
                self.stats.ok()
            else:
                self.stats.failed(f"HTTP {code}")
        return code, body


def build_application(
    token: str,
    handlers: Handlers,
    proxy: str = TELEGRAM_PROXY,
    *,
    stats: PollStats | None = None,
    request: BaseRequest | None = None,
    poll_request: BaseRequest | None = None,
) -> Application[Any, Any, Any, Any, Any, Any]:
    """``request``/``poll_request`` default to ``HTTPXRequest`` through the proxy (tests inject fakes)."""
    poller: BaseRequest = poll_request or HTTPXRequest(proxy=proxy, read_timeout=40.0)
    application = (
        Application.builder()
        .token(token)
        .request(request or HTTPXRequest(proxy=proxy))
        .get_updates_request(PollTrackingRequest(poller, stats or handlers.ctx.poll_stats))
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
        self.sender = TelegramSender(application.bot)
        self.ui = ui_of(ctx)
        self.worker = OutboxWorker(ctx.outbox, self.sender, self._on_sent, batch=1, on_failed=self._on_failed,
                                   prepare=self.ui.topics.prepare)  # fmt: skip
        self._task: asyncio.Task[None] | None = None
        self._connect_task: asyncio.Task[None] | None = None
        self._lifecycle_seen: tuple[str, str] | None = None

    async def _on_sent(self, row: OutboxRow, result: dict[str, Any]) -> None:
        await self.publisher.on_sent(row, result)
        self.ui.dashboard.on_sent(row, result)

    async def _on_failed(self, row: OutboxRow, error: SendError) -> bool:
        """Permanent failures with a defined recovery (§14.20.5)."""
        text = str(error).lower()
        if THREAD_NOT_FOUND in text and row.thread_key is not None:
            return await self.ui.topics.on_thread_not_found(row, self.sender.call)
        classic = row.payload.get("_classic")
        if isinstance(classic, dict) and "bad request" in text:
            ui_rich.mark_unavailable(self.ui.prefs)
            log.warning("rich view refused (%s); classic for 24 h", row.method, extra={"event": "rich_fallback"})
            if row.payload.get("_edit_fallback"):
                return self._edit_fallback(row, classic)
            self.ctx.outbox.requeue(row, "sendMessage", classic, thread_key=row.thread_key)
            return True
        key = row.dedupe_key or ""
        if key.startswith("stub:") and key.endswith(":moved"):  # the stub edit failed too: delete it
            mid = row.payload.get("message_id")
            self.ctx.outbox.enqueue("deleteMessage", {"chat_id": row.payload.get("chat_id"), "message_id": mid},
                                    dedupe_key=f"stub:{mid}:delete")  # fmt: skip
            return True
        return self.ui.dashboard.on_failed(row, error)

    def _edit_fallback(self, row: OutboxRow, classic: dict[str, Any]) -> bool:
        """A failed rich edit (BA-9): the classic view is sent as a new reply, the old message is stubbed
        "moved ↓" and its view row goes stale; the view's tokens are re-bound to the new message."""
        old = int(row.payload.get("message_id") or 0)
        payload = {k_: v for k_, v in classic.items() if k_ != "message_id"}
        payload["reply_parameters"] = {"message_id": old, "allow_sending_without_reply": True}
        tokens = [b_["callback_data"][2:] for r in (classic.get("reply_markup") or {}).get("inline_keyboard", [])
                  for b_ in r if "callback_data" in b_]  # fmt: skip
        stored = db.one(self.ctx.conn, "SELECT view_json FROM tg_outbox WHERE id = ?", (row.id,))
        view = json.loads(stored["view_json"]) if stored is not None and stored["view_json"] else None
        with db.transaction(self.ctx.conn):
            if tokens:
                marks = ",".join("?" * len(tokens))
                self.ctx.conn.execute(f"UPDATE ui_tokens SET tg_message_id = NULL WHERE token IN ({marks})",  # noqa: S608
                                      tokens)  # fmt: skip
            self.ctx.conn.execute("UPDATE tg_views SET stale = 1, updated_at = ? WHERE message_id = ?",
                                  (to_iso(self.ctx.clock.now()), old))  # fmt: skip
            self.ctx.outbox.enqueue("sendMessage", payload, dedupe_key=f"view:{new_token()}",
                                    bind_tokens=tokens or None, view=view)  # fmt: skip
            self.ctx.outbox.enqueue("editMessageText", {"chat_id": classic.get("chat_id"), "message_id": old,
                                                        "rich_message": {"blocks": [{"type": "paragraph",
                                                                                     "text": "moved ↓"}],
                                                                         "skip_entity_detection": True}},
                                    dedupe_key=f"stub:{old}:moved")  # fmt: skip
        return True

    async def start(self) -> None:
        """Never blocks or fails the gatekeeper start: with the network down (laptop offline,
        proxy restarting, after sleep) the bot connects in the background with backoff (CR-001
        item 1). Cards and notes queue in the persistent outbox meanwhile."""
        load_plugins()
        self.ctx.on_approval_created = self.publisher.publish
        self.ctx.card_recover = self.publisher.recover
        for svc in self.ctx.decisions.all():
            svc.on_disarmed = self.publisher.render_s1
        self._connect_task = asyncio.create_task(self._connect())

    async def _connect(self) -> None:
        delay = CONNECT_BACKOFF_MIN_S
        while True:
            try:
                await self.application.initialize()
                try:
                    self.publisher.recover()  # delta 8: before polling starts
                except Exception:
                    log.exception("card recovery failed", extra={"event": "card_recover_failed"})
                break
            except NetworkError as exc:  # incl. TimedOut: transient, retry with backoff
                self.ctx.poll_stats.failed(f"initialize: {type(exc).__name__}: {exc.message}")
                await asyncio.sleep(delay)
                delay = min(CONNECT_BACKOFF_MAX_S, delay * 2)
            except TelegramError as exc:  # InvalidToken, Forbidden: permanent, the front-end stays off
                log.error("telegram disabled: %s", type(exc).__name__, extra={"event": "telegram_disabled"})
                return
        await self.workspace()
        await self.application.start()
        assert self.application.updater is not None
        await self.application.updater.start_polling(allowed_updates=ALLOWED_UPDATES, drop_pending_updates=False)
        self._task = asyncio.create_task(self._outbox_loop())

    async def workspace(self) -> None:
        """Topics (or flat mode), the owner's menus, the short description and the dashboard (§14.10)."""
        try:
            await self.ui.topics.bootstrap(self.sender.call)
            await self.ui.scopes.apply(self.sender.call)
            await self.ui.scopes.set_description(self.sender.call, ONLINE)
            self.ui.dashboard.tick(force=True)
        except Exception:
            log.exception("telegram workspace setup failed", extra={"event": "telegram_workspace_failed"})

    async def on_tick(self) -> None:
        """Once per outbox loop: menus follow the control state, the dashboard is edited when due, and a
        full (graceful) down sets the offline short description before the outbox drains (§14.10.3)."""
        await self.ui.scopes.maybe_apply(self.sender.call)
        info = self.ctx.control.lifecycle()
        seen = (info.state, info.scope) if info is not None else ("up", "")
        if seen != self._lifecycle_seen:
            self._lifecycle_seen = seen
            graceful = info is not None and info.state == "down" and info.scope != "workers"
            text = offline_description(self.ctx.clock.now().strftime("%H:%M")) if graceful else ONLINE
            await self.ui.scopes.set_description(self.sender.call, text)
            self.ui.dashboard.tick(force=True)
        self.ui.dashboard.tick()

    async def _outbox_loop(self) -> None:
        while True:
            try:
                await self.on_tick()
            except Exception:
                log.exception("telegram tick failed", extra={"event": "telegram_tick_failed"})
            try:
                await self.worker.run_once()
            except Exception:
                log.exception("outbox loop failed", extra={"event": "outbox_failed"})
            await asyncio.sleep(OUTBOX_INTERVAL_SECONDS)

    async def stop(self) -> None:
        if self._connect_task is not None and not self._connect_task.done():
            self._connect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._connect_task
        if self._task is not None:
            self._task.cancel()
        if self.application.updater is not None and self.application.updater.running:
            await self.application.updater.stop()
        if self.application.running:
            await self.application.stop()
        await self.application.shutdown()
