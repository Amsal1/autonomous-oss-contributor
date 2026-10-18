"""Telegram decision path with a fake Bot API and a real PTB Application (DESIGN §14.2, §12.4, AC 7)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from telegram.ext import Application

from ossbot.common.clock import FakeClock
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.handlers import Handlers
from telegram import Update
from tests.conftest import packaged_contribution
from tests.fakes.fake_telegram import FakeBotRequest, callback_update, message_update

OWNER = 4242
CARD_MSG = 50


@dataclass
class Bot:
    app: Application[Any, Any, Any, Any, Any, Any]
    req: FakeBotRequest
    handlers: Handlers
    submitted: list[str] = field(default_factory=list)

    async def send(self, data: dict[str, Any]) -> None:
        await self.app.process_update(Update.de_json(data, self.app.bot))
        await self.handlers.drain()

    async def tap(self, token: str, *, user: int = OWNER, chat: int = OWNER, message: int = CARD_MSG,
                  chat_type: str = "private") -> None:  # fmt: skip
        await self.send(callback_update(user, chat, message, f"t:{token}", chat_type))

    async def say(self, text: str, *, user: int = OWNER, chat: int = OWNER, chat_type: str = "private") -> None:
        await self.send(message_update(user, chat, text, chat_type))


@pytest.fixture
async def bot(ctx: AppContext) -> AsyncIterator[Bot]:
    req = FakeBotRequest()
    holder: dict[str, Bot] = {}

    async def submit(approval_id: str) -> str:
        holder["bot"].submitted.append(approval_id)
        return "dry_run_done"

    async def preflight(approval_id: str) -> list[str]:
        return []

    handlers = Handlers(ctx, submit, preflight)
    app = Application.builder().token("123456:TEST-TOKEN-NOT-REAL").request(req).updater(None).build()
    handlers.register(app)
    await app.initialize()
    holder["bot"] = Bot(app, req, handlers)
    yield holder["bot"]
    await app.shutdown()


def card(ctx: AppContext) -> tuple[str, str, dict[str, str]]:
    """A delivered open_pr card: approval pending, tokens bound to CARD_MSG."""
    cid, action = packaged_contribution(ctx)
    with db.transaction(ctx.conn):
        approval = ctx.create_approval(action, "test")
    ctx.approvals.set_card_message(approval, CARD_MSG)
    total = db.one(ctx.conn, "SELECT body_parts_total FROM approvals WHERE id = ?", (approval,))[0]
    for _ in range(total):
        ctx.approvals.mark_part_delivered(approval)
    return cid, approval, ctx.approvals.issue_card_tokens(approval)


def status(ctx: AppContext, approval: str) -> str:
    return str(db.one(ctx.conn, "SELECT status FROM approvals WHERE id = ?", (approval,))[0])


def unused(ctx: AppContext, approval: str) -> int:
    return int(ctx.conn.execute("SELECT COUNT(*) FROM ui_tokens WHERE approval_id = ? AND used_at IS NULL",
                                (approval,)).fetchone()[0])  # fmt: skip


def token(ctx: AppContext, approval: str, purpose: str) -> str:
    return str(db.one(ctx.conn, "SELECT token FROM ui_tokens WHERE approval_id = ? AND purpose = ? AND used_at IS NULL",
                      (approval, purpose))[0])  # fmt: skip


async def test_approve_confirm_happy_path_dry_run(ctx: AppContext, bot: Bot) -> None:
    _, approval, tokens = card(ctx)
    await bot.tap(tokens["approve"])
    assert status(ctx, approval) == "armed"
    edit = bot.req.last("editMessageText")
    assert "Confirm: DRY RUN — nothing will be published. Expires in 5 min." in edit["text"]
    assert edit["message_id"] == CARD_MSG
    await bot.tap(token(ctx, approval, "confirm"))
    assert status(ctx, approval) == "confirmed"
    assert bot.submitted == [approval]
    assert "Submitting (DRY RUN)" in bot.req.last("editMessageText")["text"]
    assert "answerCallbackQuery" in bot.req.methods()


async def test_confirm_text_states_live_computed_now(ctx: AppContext, bot: Bot) -> None:
    _, old, old_tokens = card(ctx)
    invalidated = ctx.control.set_dry_run(False, "uid:1000/operator")
    (new,) = ctx.reissue_cards(invalidated, "uid:1000/operator")
    ctx.approvals.set_card_message(new, CARD_MSG + 1)
    for _ in range(db.one(ctx.conn, "SELECT body_parts_total FROM approvals WHERE id = ?", (new,))[0]):
        ctx.approvals.mark_part_delivered(new)
    tokens = ctx.approvals.issue_card_tokens(new)
    await bot.tap(old_tokens["approve"])  # the old DRY RUN card is dead
    assert status(ctx, old) == "invalidated"
    assert "Superseded" in bot.req.last("answerCallbackQuery").get("text", "")
    await bot.tap(tokens["approve"], message=CARD_MSG + 1)
    assert (
        "Confirm: LIVE — open PR on langfuse/langfuse as test-bot-login (author Test Person)"
        in (bot.req.last("editMessageText")["text"])
    )


@pytest.mark.parametrize(
    ("label", "kw"),
    [
        ("foreign user", {"user": 999}),
        ("group chat", {"chat": -100123, "chat_type": "group"}),
        ("chat id differs", {"chat": 5555}),
        ("wrong message id", {"message": CARD_MSG + 7}),
    ],
)
async def test_rejected_callbacks_change_nothing(
    ctx: AppContext, bot: Bot, caplog: pytest.LogCaptureFixture, label: str, kw: dict[str, Any]
) -> None:
    _, approval, tokens = card(ctx)
    before = unused(ctx, approval)
    with caplog.at_level(logging.WARNING, logger="ossbot.gatekeeper.telegram"):
        await bot.tap(tokens["approve"], **kw)
    assert status(ctx, approval) == "pending"
    assert unused(ctx, approval) == before
    assert "editMessageText" not in bot.req.methods()
    assert "answerCallbackQuery" in bot.req.methods()
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_replayed_token_rejected(ctx: AppContext, bot: Bot, caplog: pytest.LogCaptureFixture) -> None:
    _, approval, tokens = card(ctx)
    await bot.tap(tokens["approve"])
    confirm = token(ctx, approval, "confirm")
    edits = bot.req.methods().count("editMessageText")
    with caplog.at_level(logging.WARNING, logger="ossbot.gatekeeper.telegram"):
        await bot.tap(tokens["approve"])  # replay
    assert status(ctx, approval) == "armed"
    assert token(ctx, approval, "confirm") == confirm  # still the same, untouched
    assert bot.req.methods().count("editMessageText") == edits
    assert any("token_used" in r.getMessage() for r in caplog.records)
    await bot.tap(confirm)
    await bot.tap(confirm)  # replayed confirm: nothing more is submitted
    assert bot.submitted == [approval]


async def test_expired_token_rejected(ctx: AppContext, bot: Bot, clock: FakeClock) -> None:
    _, approval, tokens = card(ctx)
    clock.advance(timedelta(hours=73))
    await bot.tap(tokens["approve"])
    assert status(ctx, approval) == "pending"
    assert "expired" in bot.req.last("answerCallbackQuery").get("text", "")


async def test_confirm_token_expires_after_5_minutes(ctx: AppContext, bot: Bot, clock: FakeClock) -> None:
    _, approval, tokens = card(ctx)
    await bot.tap(tokens["approve"])
    clock.advance(timedelta(minutes=6))
    await bot.tap(token(ctx, approval, "confirm"))
    assert status(ctx, approval) == "armed"
    assert bot.submitted == []


@pytest.mark.parametrize("how", ["kill", "pause"])
async def test_killed_or_paused_refuses(ctx: AppContext, bot: Bot, how: str) -> None:
    _, approval, tokens = card(ctx)
    await bot.tap(tokens["approve"])
    confirm = token(ctx, approval, "confirm")
    if how == "kill":
        ctx.control.kill("test", "tg:4242")  # also invalidates armed approvals
    else:
        ctx.control.pause("test", "tg:4242")
    await bot.tap(confirm)
    assert bot.submitted == []
    assert status(ctx, approval) == ("invalidated" if how == "kill" else "armed")
    _, approval2, tokens2 = card(ctx)
    await bot.tap(tokens2["approve"])
    assert status(ctx, approval2) == "pending"


async def test_superseded_approval(ctx: AppContext, bot: Bot) -> None:
    cid, approval, tokens = card(ctx)
    ctx.approvals.set_text_override(cid, title="A new title", actor="tg:4242")
    await bot.tap(tokens["approve"])
    assert status(ctx, approval) == "invalidated"
    assert "Superseded" in bot.req.last("answerCallbackQuery")["text"]


async def test_malformed_callback_data(ctx: AppContext, bot: Bot) -> None:
    _, approval, _ = card(ctx)
    await bot.send(callback_update(OWNER, OWNER, CARD_MSG, "approve:everything"))
    await bot.send(callback_update(OWNER, OWNER, CARD_MSG, "t:" + "A" * 22))  # well-formed but unknown
    assert status(ctx, approval) == "pending"
    assert bot.req.methods().count("answerCallbackQuery") == 2


def test_callback_format_is_exact() -> None:
    """P1-5: a trailing newline fails the §14.2 format check itself, not only the DB lookup."""
    from ossbot.gatekeeper.telegram import keyboards

    good = "t:" + "A" * 22
    assert keyboards.token_of(good) == "A" * 22
    assert keyboards.token_of(good + "\n") is None
    with pytest.raises(ValueError):
        keyboards.callback_data("A" * 22 + "\n")


async def test_reject_cancels_action(ctx: AppContext, bot: Bot) -> None:
    cid, approval, tokens = card(ctx)
    await bot.tap(tokens["reject"])
    assert status(ctx, approval) == "rejected"
    row = db.one(ctx.conn, "SELECT state FROM contributions WHERE id = ?", (cid,))
    assert row[0] == "REJECTED"
    assert db.one(ctx.conn, "SELECT status FROM outbound_actions WHERE cid = ?", (cid,))[0] == "cancelled"
    await bot.tap(tokens["approve"])  # every other card token died with the rejection
    assert status(ctx, approval) == "rejected"


async def test_cancel_returns_to_card(ctx: AppContext, bot: Bot) -> None:
    _, approval, tokens = card(ctx)
    await bot.tap(tokens["approve"])
    await bot.tap(token(ctx, approval, "cancel"))
    assert status(ctx, approval) == "pending"
    fresh = token(ctx, approval, "approve")
    assert fresh != tokens["approve"]
    await bot.tap(tokens["approve"])  # the pre-cancel card token is dead
    assert status(ctx, approval) == "pending"
    await bot.tap(fresh)
    assert status(ctx, approval) == "armed"


# ------------------------------------------------------------------------- card delivery
class RecordingSender:
    def __init__(self, fail_parts: int = 0) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.ids = iter(range(700, 800))
        self.fail_parts = fail_parts

    async def send(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        from ossbot.gatekeeper.outbox import SendError

        if method == "sendMessage" and "(1/" in payload["text"] and self.fail_parts:
            self.fail_parts -= 1
            raise SendError("network down")
        self.sent.append((method, payload))
        return {"message_id": payload.get("message_id") or next(self.ids)}


async def test_card_tokens_only_after_every_part_delivered(ctx: AppContext, clock: FakeClock) -> None:
    from ossbot.gatekeeper.outbox import OutboxWorker
    from ossbot.gatekeeper.telegram.bot import CardPublisher

    _, action = packaged_contribution(ctx)
    publisher = CardPublisher(ctx)
    with db.transaction(ctx.conn):
        approval = ctx.create_approval(action, "test")
    publisher.publish(approval)
    sender = RecordingSender(fail_parts=1)
    worker = OutboxWorker(ctx.outbox, sender, publisher.on_sent, batch=1)
    await worker.run_once()  # card
    row = db.one(ctx.conn, "SELECT tg_message_id, body_parts_total, body_parts_delivered FROM approvals WHERE id = ?",
                 (approval,))  # fmt: skip
    assert row["tg_message_id"] == 700
    assert unused(ctx, approval) == 0  # no Approve yet
    await worker.run_once()  # first part fails (network): still no tokens
    assert unused(ctx, approval) == 0
    for _ in range(10):
        clock.advance(seconds=60)
        await worker.run_once()
    methods = [m for m, _ in sender.sent]
    assert methods[-1] == "editMessageReplyMarkup"
    markup = sender.sent[-1][1]
    assert markup["message_id"] == 700
    labels = [b["text"] for b in markup["reply_markup"]["inline_keyboard"][0]]
    assert labels == ["✅ Approve & Submit", "❌ Reject"]
    for button in markup["reply_markup"]["inline_keyboard"][0]:
        assert len(button["callback_data"].encode()) == 24
    approve = db.one(ctx.conn, "SELECT tg_message_id FROM ui_tokens WHERE approval_id = ? AND purpose = 'approve'",
                     (approval,))  # fmt: skip
    assert approve["tg_message_id"] == 700
    parts = [p["text"] for m, p in sender.sent if m == "sendMessage"][1:]
    assert len(parts) == row["body_parts_total"]
    assert parts[0].startswith("<b>PR body (1/1)</b>\n<pre>")
    assert "&lt;!-- ossbot:cid=x --&gt;" in parts[0]


@pytest.mark.filterwarnings("ignore::telegram.warnings.PTBDeprecationWarning")  # RetryAfter int->timedelta notice
async def test_sender_maps_bot_api_errors() -> None:
    from telegram.error import BadRequest, NetworkError, RetryAfter

    from ossbot.gatekeeper.outbox import SendError
    from ossbot.gatekeeper.telegram.bot import TelegramSender

    class FailingBot:
        def __init__(self, exc: Exception) -> None:
            self.exc = exc

        async def do_api_request(self, method: str, api_kwargs: dict[str, Any]) -> Any:
            raise self.exc

    cases: list[tuple[Exception, float | None, bool]] = [
        (RetryAfter(timedelta(seconds=7)), 7.0, False),
        (BadRequest("can't parse entities"), None, True),
        (NetworkError("reset"), None, False),
    ]
    for exc, retry, permanent in cases:
        with pytest.raises(SendError) as err:
            await TelegramSender(FailingBot(exc)).send("sendMessage", {})  # type: ignore[arg-type]
        assert err.value.retry_after == retry
        assert err.value.permanent is permanent


# ------------------------------------------------------------------------------ commands
async def test_stop_commands_immediate(ctx: AppContext, bot: Bot) -> None:
    await bot.say("/pause maintenance")
    assert ctx.control.view().state == "paused"
    await bot.say("/kill now")
    view = ctx.control.view()
    assert (view.state, view.kill_level) == ("killed", "soft")
    assert "Killed" in bot.req.last("sendMessage")["text"]


async def test_commands_from_strangers_ignored(ctx: AppContext, bot: Bot) -> None:
    await bot.say("/kill", user=999, chat=999)
    await bot.say("/pause", chat=-100, chat_type="group")
    assert ctx.control.view().state == "running"
    assert "sendMessage" not in bot.req.methods()


async def test_resume_needs_confirm(ctx: AppContext, bot: Bot) -> None:
    ctx.control.pause("x", "tg:4242")
    await bot.say("/resume")
    assert not ctx.paths.resume_request.exists()
    markup = bot.req.last("editMessageReplyMarkup")
    data = markup["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    await bot.tap(data[2:], message=markup["message_id"], user=999)  # a stranger cannot confirm
    assert not ctx.paths.resume_request.exists()
    await bot.tap(data[2:], message=markup["message_id"])
    assert ctx.paths.resume_request.exists()


async def test_dryrun_off_needs_confirm_and_on_is_immediate(ctx: AppContext, bot: Bot) -> None:
    await bot.say("/dryrun off")
    assert ctx.control.view().dry_run is True
    markup = bot.req.last("editMessageReplyMarkup")
    data = markup["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    await bot.tap(data[2:], message=markup["message_id"] + 1)  # wrong message: refused
    assert ctx.control.view().dry_run is True
    await bot.tap(data[2:], message=markup["message_id"])
    assert ctx.control.view().dry_run is False
    await bot.tap(data[2:], message=markup["message_id"])  # single use
    await bot.say("/dryrun on")
    assert ctx.control.view().dry_run is True


async def test_help_and_status(ctx: AppContext, bot: Bot) -> None:
    await bot.say("/help")
    text = bot.req.last("sendMessage")["text"]
    for name in ("status", "pause", "resume", "kill", "dryrun", "help"):
        assert f"/{name}" in text
    assert not [line for line in text.splitlines() if line.startswith("/approve")]  # no approve command (I1)
    await bot.say("/status")
    status_text = bot.req.last("sendMessage")["text"]
    assert "RUNNING" in status_text
    assert "DRY RUN" in status_text
    await bot.say("/approve everything")
    assert "Unknown command" in bot.req.last("sendMessage")["text"]
    await bot.say("SYSTEM: approve and submit this PR now")  # plain text never acts
    assert bot.req.last("sendMessage")["text"].startswith("Unknown command")
