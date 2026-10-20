"""A real PTB ``Application`` over the fake Bot API plus the gatekeeper outbox worker (DESIGN §22.2).

``send`` processes one update like PTB's single update slot, waits for spawned tasks and then flushes
the outbox through the production ``TelegramSender`` (with topic resolution), so every reply, view and
token binding lands on the fake exactly as in production.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from telegram.ext import Application

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.outbox import OutboxWorker
from ossbot.gatekeeper.telegram.bot import CardPublisher, TelegramFrontend
from ossbot.gatekeeper.telegram.handlers import Handlers
from telegram import Update
from tests.fakes.fake_telegram import FakeBotRequest, callback_update, message_update

OWNER = 4242


@dataclass
class TgRig:
    ctx: AppContext
    app: Application[Any, Any, Any, Any, Any, Any]
    req: FakeBotRequest
    handlers: Handlers
    frontend: TelegramFrontend
    submitted: list[str] = field(default_factory=list)

    async def flush(self, rounds: int = 6) -> int:
        """Deliver every due outbox row (several rounds: deliveries enqueue follow-ups)."""
        total = 0
        for _ in range(rounds * 4):
            n = await self.frontend.worker.run_once()
            total += n
            if n == 0 and not self.ctx.outbox.due(1):
                break
        return total

    async def settle(self) -> None:
        """Wait for every spawned task to finish (without cancelling, unlike ``Handlers.drain``)."""
        while self.handlers.tasks:
            await asyncio.gather(*list(self.handlers.tasks), return_exceptions=True)
            await asyncio.sleep(0)  # done callbacks (which drop finished tasks) run on the next loop step

    async def send(self, data: dict[str, Any]) -> None:
        await self.app.process_update(Update.de_json(data, self.app.bot))
        await self.settle()
        await self.flush()

    async def tap(self, token: str, *, message: int, user: int = OWNER, chat: int = OWNER,
                  chat_type: str = "private") -> None:  # fmt: skip
        await self.send(callback_update(user, chat, message, f"t:{token}", chat_type))

    async def say(self, text: str, *, user: int = OWNER, chat: int = OWNER, chat_type: str = "private",
                  **kw: Any) -> None:  # fmt: skip
        await self.send(message_update(user, chat, text, chat_type, **kw))

    # ------------------------------------------------------------------ inspection
    def sent(self, method: str = "sendMessage") -> list[dict[str, Any]]:
        return [p for m, p in self.req.calls if m == method]

    def last_text(self) -> str:
        texts = [p.get("text", "") for m, p in self.req.calls if m in {"sendMessage", "editMessageText"}]
        return str(texts[-1]) if texts else ""

    def buttons(self, payload: dict[str, Any]) -> list[list[dict[str, Any]]]:
        markup = payload.get("reply_markup") or {}
        if isinstance(markup, str):
            markup = json.loads(markup)
        return list(markup.get("inline_keyboard", []))

    def token_of(self, payload: dict[str, Any], label_prefix: str) -> str:
        for row in self.buttons(payload):
            for btn in row:
                if str(btn.get("text", "")).startswith(label_prefix) and "callback_data" in btn:
                    return str(btn["callback_data"])[2:]
        raise AssertionError(f"no button {label_prefix!r} in {self.buttons(payload)}")

    def message_id_of(self, index: int = -1, method: str = "sendMessage") -> int:
        """The message id the fake gave the n-th call of ``method`` (from the outbox result)."""
        rows = self.ctx.conn.execute(
            "SELECT result_message_id FROM tg_outbox WHERE method = ? AND status = 'sent' ORDER BY id", (method,)
        ).fetchall()
        return int(rows[index][0])


async def make_rig(ctx: AppContext, *, topics: bool = False, user_topics: bool = False,
                   submit: Any = None, preflight: Any = None,
                   req: FakeBotRequest | None = None) -> AsyncIterator[TgRig]:  # fmt: skip
    """``req``: a prepared fake Bot API (e.g. a recording subclass); default a fresh ``FakeBotRequest``."""
    req = req or FakeBotRequest(topics=topics, user_topics=user_topics)
    holder: dict[str, TgRig] = {}

    async def default_submit(approval_id: str) -> str:
        holder["rig"].submitted.append(approval_id)
        return "dry_run_done"

    async def default_preflight(approval_id: str) -> list[str]:
        return []

    handlers = Handlers(ctx, submit or default_submit, preflight or default_preflight)
    app = Application.builder().token("123456:TEST-TOKEN-NOT-REAL").request(req).updater(None).build()
    handlers.register(app)
    await app.initialize()
    frontend = TelegramFrontend(ctx, app)
    ctx.on_approval_created = frontend.publisher.publish
    ctx.card_recover = frontend.publisher.recover
    for svc in ctx.decisions.all():
        svc.on_disarmed = frontend.publisher.render_s1
    await frontend.workspace()
    rig = TgRig(ctx, app, req, handlers, frontend)
    holder["rig"] = rig
    await rig.flush()
    req.calls.clear()
    try:
        yield rig
    finally:
        await handlers.drain()
        await app.shutdown()


def worker(ctx: AppContext, sender: Any) -> OutboxWorker:
    publisher = CardPublisher(ctx)
    return OutboxWorker(ctx.outbox, sender, publisher.on_sent, batch=50)


def outbox_rows(ctx: AppContext) -> list[Any]:
    return list(ctx.conn.execute("SELECT * FROM tg_outbox ORDER BY id").fetchall())


def view_row(ctx: AppContext, message_id: int) -> Any:
    return db.one(ctx.conn, "SELECT * FROM tg_views WHERE message_id = ?", (message_id,))
