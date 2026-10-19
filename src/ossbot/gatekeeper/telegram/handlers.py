"""Telegram update handlers: the deterministic decision path (DESIGN §14.2, §14.4, §12.4, I1, I3).

* Every update passes ``auth.authorized`` first (owner, private chat); others get no reply.
* Callback data must be ``t:<22 chars>``; the token row must exist, have the right purpose, be
  bound to this message id, unused and unexpired. Rejections change nothing and log WARNING.
* Approve -> card edited to the mode-stating Confirm (LIVE / DRY RUN computed at that moment)
  -> Confirm -> live checks -> submit. Reject cancels the action. ``go`` commands confirm first.
* Nothing typed in chat (or produced by Hermes later) can reach these paths except through the
  command registry; the registry has no approve command.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from datetime import timedelta
from typing import Any, Literal

from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from ossbot.common.clock import to_iso
from ossbot.common.control_flags import Blocked
from ossbot.common.ids import new_action_id
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalError
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.proposals import ProposalRefused, ProposalService
from ossbot.gatekeeper.state_machine import Actor, TransitionError
from ossbot.gatekeeper.telegram import cards, keyboards
from ossbot.gatekeeper.telegram.auth import authorized
from ossbot.gatekeeper.telegram.commands import builtin  # noqa: F401 - registers the built-ins
from ossbot.gatekeeper.telegram.commands.registry import (
    COMMANDS,
    CONFIRM_EFFECTS,
    CONFIRMS,
    CommandContext,
    Reply,
)
from ossbot.gatekeeper.telegram.escape import b, esc
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

log = logging.getLogger(__name__)
Submit = Callable[[str], Awaitable[str]]
Preflight = Callable[[str], Awaitable[list[str]]]
COMMAND_CONFIRM_TTL = timedelta(minutes=5)
RETRY_TTL = timedelta(hours=24)
MARKER_PREFIX = "<!-- ossbot:action="
# Update handler budget (DESIGN §14.18.5, BA-2). PTB runs one update at a time, so anything slower
# than SQLite, an outbox enqueue and one direct Bot API edit is spawned as a tracked task.
TaskKind = Literal["chat_turn", "media", "telegraph", "github_read", "render", "submit", "command"]
KILL_CANCELS: frozenset[TaskKind] = frozenset({"chat_turn", "media", "telegraph", "github_read", "render"})
PREFLIGHT_TIMEOUT_S = 8.0  # the one inline await: the Confirm live preflight (timeout = live_check:timeout)
HANDLER_SLOW_S = 1.0
STOP_COMMANDS = frozenset({"kill", "pause"})
# Gate 1 decision purposes (p_ ids); the kit purposes (skip, adjust, ...) arrive with FEAT-009.
PROPOSAL_PURPOSES = frozenset({"proposal_start", "proposal_confirm", "proposal_cancel"})
# Skill activation (§14.15, §15.5; s_ ids): two taps; the kit card and the diff/ask views are PLAN item 47.
SKILL_PURPOSES = frozenset({"skill_approve", "skill_confirm", "skill_cancel", "skill_reject"})


def markup(rows: keyboards.Rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=keyboards.callback_data(tok)) for label, tok in row]
            for row in rows
        ]
    )


class Handlers:
    def __init__(self, ctx: AppContext, submit: Submit | None = None, preflight: Preflight | None = None) -> None:
        self.ctx = ctx
        self.submit = submit
        self.preflight = preflight
        self.tasks: set[asyncio.Task[Any]] = set()
        self._by_kind: dict[TaskKind, set[asyncio.Task[Any]]] = {}
        self._sessions: dict[str, asyncio.Task[Any]] = {}  # session_key -> its running chat_turn

    def register(self, application: Application[Any, Any, Any, Any, Any, Any]) -> None:
        application.add_handler(CallbackQueryHandler(self.on_callback))
        application.add_handler(MessageHandler(filters.ALL, self.on_message))

    # ----------------------------------------------------------- task budget (§14.18.5)
    def spawn(
        self,
        coro: Coroutine[Any, Any, Any],
        kind: TaskKind,
        *,
        session_key: str | None = None,
        on_done: Callable[[asyncio.Task[Any]], None] | None = None,
    ) -> asyncio.Task[Any]:
        """Run ``coro`` outside the update slot. Cleanup lives in one done callback, which asyncio runs
        for every outcome, including a cancel before the coroutine started (BA-15)."""
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        self._by_kind.setdefault(kind, set()).add(task)
        if session_key is not None:
            self._sessions[session_key] = task
        task.add_done_callback(lambda t: self._on_task_done(t, kind, session_key, on_done))
        return task

    def _on_task_done(
        self,
        task: asyncio.Task[Any],
        kind: TaskKind,
        session_key: str | None,
        on_done: Callable[[asyncio.Task[Any]], None] | None,
    ) -> None:
        """Synchronous and idempotent: every step checks before it changes."""
        self.tasks.discard(task)
        self._by_kind.get(kind, set()).discard(task)
        if session_key is not None and self._sessions.get(session_key) is task:
            del self._sessions[session_key]
        if not task.cancelled() and task.exception() is not None:
            log.error("spawned %s task failed: %s", kind, type(task.exception()).__name__,
                      extra={"event": "task_failed", "kind": kind})  # fmt: skip
        if on_done is not None:
            try:
                on_done(task)
            except Exception:
                log.exception("on_done of a %s task failed", kind, extra={"event": "task_on_done_failed"})

    def session_busy(self, session_key: str) -> bool:
        return session_key in self._sessions

    def cancel(self, kinds: frozenset[TaskKind] | set[TaskKind]) -> int:
        """Cancel the running tasks of ``kinds``; returns how many were cancelled."""
        n = 0
        for kind in kinds:
            for task in list(self._by_kind.get(kind, ())):
                if not task.done():
                    task.cancel()
                    n += 1
        return n

    async def drain(self) -> None:
        """Shutdown: cancel the kill-cancellable kinds, then await every task (submit included)."""
        self.cancel(KILL_CANCELS)
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    async def _timed(self, name: str, threshold_s: float, coro: Awaitable[None]) -> None:
        started = time.monotonic()
        try:
            await coro
        finally:
            elapsed = time.monotonic() - started
            if elapsed > threshold_s:
                log.warning("handler %s took %d ms (budget %d ms)", name, elapsed * 1000, threshold_s * 1000,
                            extra={"event": "handler_slow", "handler": name, "ms": int(elapsed * 1000),
                                   "threshold_ms": int(threshold_s * 1000)})  # fmt: skip

    # -------------------------------------------------------------------- helpers
    def _peek(self, token: str) -> Any:
        return db.one(self.ctx.conn, "SELECT * FROM ui_tokens WHERE token = ?", (token,))

    def _rejection_text(self, exc: ApprovalError, row: Any) -> str:
        if exc.code == "superseded" or (row is not None and self._approval_status(row) == "invalidated"):
            return "Superseded: content or mode changed since this card; review the new card."
        if exc.code in {"token_expired", "approval_expired"}:
            return "This button has expired."
        if exc.code == "confirm_refused":
            return "Paused or killed: nothing was confirmed. /resume first."
        return "This button is no longer valid."

    def _approval_status(self, row: Any) -> str | None:
        if row["approval_id"] is None:
            return None
        appr = db.one(self.ctx.conn, "SELECT status FROM approvals WHERE id = ?", (row["approval_id"],))
        return str(appr["status"]) if appr else None

    def _card_text(self, approval_id: str) -> str:
        try:
            return cards.render(cards.view_for_approval(self.ctx.conn, approval_id)).card
        except KeyError:
            return "(card unavailable)"

    async def _edit(self, query: Any, text: str, rows: keyboards.Rows | None = None) -> None:
        try:
            await query.edit_message_text(
                text=text[: cards.MESSAGE_MAX], parse_mode=ParseMode.HTML, reply_markup=markup(rows) if rows else None
            )
        except TelegramError as exc:
            log.warning("edit failed: %s", exc, extra={"event": "telegram_edit_failed"})

    async def _send_reply(self, chat_id: int, reply: Reply, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = await context.bot.send_message(chat_id=chat_id, text=reply.text, parse_mode=ParseMode.HTML)
        if reply.confirm is None:
            return
        token = self.ctx.approvals.mint_token(
            reply.confirm.purpose, chat_id=chat_id, message_id=msg.message_id,
            expires_at=self.ctx.clock.now() + COMMAND_CONFIRM_TTL, arg=reply.confirm.arg,
        )  # fmt: skip
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=msg.message_id,
            reply_markup=markup(keyboards.single_row(reply.confirm.label, token)),
        )

    # ------------------------------------------------------------------ callbacks
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        token = keyboards.token_of(query.data) if query is not None else None
        row = self._peek(token) if token is not None else None
        purpose = str(row["purpose"]) if row is not None else "unknown"
        threshold = PREFLIGHT_TIMEOUT_S + 1.0 if purpose == "confirm" else HANDLER_SLOW_S
        await self._timed(f"callback:{purpose}", threshold, self._on_callback(update, context))

    async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        if not authorized(update, self.ctx.telegram_chat_id):
            await query.answer()
            return
        token = keyboards.token_of(query.data)
        message = query.message
        if token is None or message is None:
            log.warning("callback rejected: malformed data", extra={"event": "callback_rejected"})
            await query.answer()
            return
        chat_id, message_id = message.chat.id, message.message_id
        user_id = query.from_user.id
        row = self._peek(token)
        if row is None:
            log.warning("callback rejected: unknown token", extra={"event": "callback_rejected"})
            await query.answer("This button is no longer valid.")
            return
        purpose = str(row["purpose"])
        try:
            if purpose == "approve":
                await self._approve(query, token, user_id, chat_id, message_id)
            elif purpose == "confirm":
                await self._confirm(query, token, row, user_id, chat_id, message_id)
            elif purpose == "cancel":
                self.ctx.approvals.cancel(token, tg_user_id=user_id, chat_id=chat_id, message_id=message_id)
                tokens = self.ctx.approvals.rotate_card_tokens(row["approval_id"])
                kind = self._kind_of(row["approval_id"])
                await self._edit(query, self._card_text(row["approval_id"]), keyboards.card_rows(kind, tokens))
                await query.answer("Cancelled; nothing was submitted.")
            elif purpose == "reject":
                await self._reject(query, token, user_id, chat_id, message_id)
            elif purpose == "retry":
                await self._retry(query, token, row, user_id, chat_id, message_id)
            elif purpose in PROPOSAL_PURPOSES:
                await self._proposal(query, token, purpose, row, user_id, chat_id, message_id)
            elif purpose in SKILL_PURPOSES:
                await self._skill(query, token, purpose, row, user_id, chat_id, message_id)
            elif purpose in CONFIRMS:
                with db.transaction(self.ctx.conn):
                    self.ctx.approvals.use_token(token, (purpose,), chat_id=chat_id, message_id=message_id)
                effect = CONFIRMS[purpose](CommandContext(self.ctx, user_id), row["arg"])
                if CONFIRM_EFFECTS.get(purpose, "local") == "io":  # BA-18: token used, ⏳ inline, effect spawned
                    await self._edit(query, "⏳ Working…")
                    await query.answer()
                    self.spawn(self._finish_confirm(query, effect), "command")
                else:
                    reply = await effect
                    await self._edit(query, reply.text)
                    await query.answer()
            else:
                await query.answer("Not available yet.")
        except ApprovalError as exc:
            log.warning(
                "callback rejected: %s (purpose %s, user %s)", exc.code, purpose, user_id,
                extra={"event": "callback_rejected"},
            )  # fmt: skip
            await query.answer(self._rejection_text(exc, row))

    async def _proposal(
        self, query: Any, token: str, purpose: str, row: Any, user_id: int, chat_id: int, message_id: int
    ) -> None:
        """Gate 1 (§14.11.15): ✅ Start work -> Confirm -> PROPOSAL_APPROVED. Two taps, message-bound
        single-use tokens; the binding is recomputed at each tap and right before the effect."""
        svc = self.ctx.decisions.proposals
        pid = str(row["approval_id"])
        tap = {"tg_user_id": user_id, "chat_id": chat_id, "message_id": message_id}
        if purpose == "proposal_start":
            arm = svc.approve(token, **tap)
            text = f"{svc.card_view(pid).card}\n\n{b(arm.confirm_text)}"
            await self._edit(query, text, keyboards.confirm_rows(arm.confirm_token, arm.cancel_token))
        elif purpose == "proposal_confirm":
            svc.confirm(token, **tap)
            try:
                result = ProposalService(self.ctx).start(pid, f"tg:{user_id}")
            except (ProposalRefused, TransitionError) as exc:
                await self._edit(query, f"{svc.card_view(pid).card}\n\n❌ Not started: {esc(str(exc)[:200])}")
                await query.answer("Not started.")
                return
            note = f"🛠 {b('Started')} · class {esc(result['approved_class'])}. The PR still needs its own approval."
            await self._edit(query, f"{svc.card_view(pid).card}\n\n{note}")
        else:
            svc.cancel(token, **tap)
            tokens = svc.rotate_card_tokens(pid)
            await self._edit(query, svc.card_view(pid).card, svc.card_rows(pid, tokens))
        await query.answer()

    async def _skill(
        self, query: Any, token: str, purpose: str, row: Any, user_id: int, chat_id: int, message_id: int
    ) -> None:
        """✅ Activate -> Confirm -> root copies exactly the bound bytes; ❌ Reject drops the staged skill.
        The binding (digest, file hashes, declared tools, tier) is recomputed at each tap and in ``consume``."""
        svc = self.ctx.decisions.skills
        sid = str(row["approval_id"])
        tap = {"tg_user_id": user_id, "chat_id": chat_id, "message_id": message_id}
        if purpose == "skill_approve":
            arm = svc.approve(token, **tap)
            text = f"{svc.card_view(sid).card}\n\n{b(arm.confirm_text)}"
            await self._edit(query, text, keyboards.confirm_rows(arm.confirm_token, arm.cancel_token))
        elif purpose == "skill_confirm":
            svc.confirm(token, **tap)
            self.ctx.skill_reviews.request_activation(sid, f"tg:{user_id}")
            await self._edit(
                query, f"{svc.card_view(sid).card}\n\n✅ {b('Activating')}: root installs the approved files."
            )
        elif purpose == "skill_reject":
            svc.reject(token, **tap)
            self.ctx.skill_reviews.request_rejection(sid, f"tg:{user_id}")
            await self._edit(query, f"{svc.card_view(sid).card}\n\n❌ {b('Rejected')}: the staged skill is dropped.")
        else:
            svc.cancel(token, **tap)
            tokens = svc.rotate_card_tokens(sid)
            await self._edit(query, svc.card_view(sid).card, svc.card_rows(sid, tokens))
        await query.answer()

    async def _finish_confirm(self, query: Any, effect: Awaitable[Reply]) -> None:
        try:
            reply = await effect
        except (Blocked, ApprovalError, TransitionError) as exc:
            reply = Reply(f"Refused: {esc(str(exc)[:300])}")
        await self._edit(query, reply.text)

    def _kind_of(self, approval_id: str) -> Any:
        row = db.one(
            self.ctx.conn,
            "SELECT o.kind FROM approvals a JOIN outbound_actions o ON o.id = a.action_id WHERE a.id = ?",
            (approval_id,),
        )
        if row is None:
            raise ApprovalError("unknown_approval", approval_id)
        return row["kind"]

    def _not_up(self) -> str | None:
        """CR-001: no new submissions while draining/down; approvals stay valid (not invalidated)."""
        info = self.ctx.control.lifecycle()
        return None if info is None else info.state

    async def _approve(self, query: Any, token: str, user_id: int, chat_id: int, message_id: int) -> None:
        if not self.ctx.approvals.system_running():
            log.warning("approve refused while paused/killed", extra={"event": "callback_rejected"})
            await query.answer("Paused or killed: nothing was approved. /resume first.")
            return
        lifecycle = self._not_up()
        if lifecycle is not None:
            log.warning("approve refused while %s", lifecycle, extra={"event": "callback_rejected"})
            await query.answer(f"ossbot is {lifecycle}: nothing was approved. The card stays valid; retry after up.")
            return
        arm = self.ctx.approvals.approve(token, tg_user_id=user_id, chat_id=chat_id, message_id=message_id)
        text = f"{self._card_text(arm.approval_id)}\n\n{b(arm.confirm_text)}"
        await self._edit(query, text, keyboards.confirm_rows(arm.confirm_token, arm.cancel_token))
        await query.answer()

    async def _confirm(self, query: Any, token: str, row: Any, user_id: int, chat_id: int, message_id: int) -> None:
        approval_id = str(row["approval_id"])
        # Every token rule first (read-only), so a replayed or foreign tap triggers nothing at all.
        self.ctx.approvals.check_token(token, ("confirm",), chat_id=chat_id, message_id=message_id)
        if not self.ctx.approvals.system_running():
            raise ApprovalError("confirm_refused", "paused or killed")
        lifecycle = self._not_up()
        if lifecycle is not None:
            log.warning("confirm refused while %s", lifecycle, extra={"event": "callback_rejected"})
            await query.answer(f"ossbot is {lifecycle}: nothing was submitted. Confirm again after up.")
            return
        if self.preflight is not None:
            # §14.18.5 item 5: the one inline await, bounded; a timeout is the failed check
            # live_check:timeout (the existing invalidation path, S12 with Retry).
            try:
                failed = await asyncio.wait_for(self.preflight(approval_id), PREFLIGHT_TIMEOUT_S)
            except TimeoutError:
                failed = ["timeout"]
            if failed:
                self.ctx.approvals.invalidate_one(approval_id, "live_check:" + ",".join(failed), f"tg:{user_id}")
                await self._edit(
                    query, f"{self._card_text(approval_id)}\n\n❌ Live checks failed: {esc(', '.join(failed))}"
                )
                await query.answer("Live checks failed; nothing was submitted.")
                return
        binding = self.ctx.approvals.confirm(token, tg_user_id=user_id, chat_id=chat_id, message_id=message_id)
        mode = "DRY RUN" if binding["dry_run"] else "LIVE"
        await self._edit(query, f"{self._card_text(approval_id)}\n\n⏳ {b(f'Submitting ({mode})…')}")
        await query.answer()
        if self.submit is not None:
            self.spawn(self._run_submit(approval_id), "submit")  # not cancelled by /kill (Phase 1 semantics)

    async def _run_submit(self, approval_id: str) -> None:
        assert self.submit is not None
        try:
            await self.submit(approval_id)
        except ApprovalError as exc:
            log.warning("submit refused: %s", exc.code, extra={"event": "submit_refused"})
            self.ctx.notify(f"Submission refused ({esc(exc.code)}); nothing was published.")
        except Exception:
            log.exception("submission crashed", extra={"event": "submit_crashed"})
            self.ctx.notify(
                "🚨 Submission crashed; restart reconciliation will resolve it. Nothing is retried automatically."
            )

    async def _reject(self, query: Any, token: str, user_id: int, chat_id: int, message_id: int) -> None:
        ctx = self.ctx
        actor = f"tg:{user_id}"
        with db.transaction(ctx.conn):
            action_id = ctx.approvals.reject(token, tg_user_id=user_id, chat_id=chat_id, message_id=message_id)
            action = db.one(ctx.conn, "SELECT * FROM outbound_actions WHERE id = ?", (action_id,))
            assert action is not None
            contrib = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (action["cid"],))
            assert contrib is not None
            ctx.conn.execute("UPDATE outbound_actions SET status = 'cancelled' WHERE id = ?", (action_id,))
            state = State(contrib["state"])
            kind = action["kind"]
            if kind == "open_pr" and state is State.AWAITING_APPROVAL:
                ctx.state_machine.transition(
                    contrib["id"], state, State.REJECTED, Actor.HUMAN, {"reason": "rejected"}, actor
                )
            elif kind == "push_update" and state is State.AWAITING_APPROVAL:
                ctx.state_machine.transition(
                    contrib["id"], state, State.WATCHING, Actor.HUMAN, {"reason": "fix_rejected"}, actor
                )
            elif kind == "issue_claim" and state is State.VETTED:
                ctx.state_machine.transition(
                    contrib["id"], state, State.SKIPPED, Actor.GATEKEEPER, {"reason": "claim_rejected"}, actor
                )
            elif kind == "issue_claim" and state is State.PACKAGING:
                ctx.state_machine.transition(
                    contrib["id"], state, State.FAILED, Actor.GATEKEEPER, {"error_class": "claim_rejected"}, actor
                )
        await self._edit(query, f"{self._card_text_safe(action_id)}\n\n❌ {b('Rejected')}; nothing was published.")
        await query.answer("Rejected.")

    def _card_text_safe(self, action_id: str) -> str:
        try:
            return cards.render(cards.view_for_action(self.ctx.conn, action_id, db.current_dry_run(self.ctx.conn))).card
        except KeyError:
            return ""

    async def _retry(self, query: Any, token: str, row: Any, user_id: int, chat_id: int, message_id: int) -> None:
        """Retry = a fresh card for the same content; it needs the full two-tap approval again."""
        ctx = self.ctx
        actor = f"tg:{user_id}"
        with db.transaction(ctx.conn):
            ctx.approvals.use_token(token, ("retry",), chat_id=chat_id, message_id=message_id)
            old = db.one(ctx.conn, "SELECT * FROM outbound_actions WHERE id = ?", (row["arg"],))
            if old is None or old["status"] != "failed":
                raise ApprovalError("retry_unavailable")
            contrib = db.one(ctx.conn, "SELECT state FROM contributions WHERE id = ?", (old["cid"],))
            assert contrib is not None
            payload: dict[str, Any] = json.loads(old["payload_json"])
            new_id = new_action_id()
            if old["kind"] in {"comment", "issue_claim"}:
                body = str(payload["body"])
                payload["body"] = body.replace(f"{MARKER_PREFIX}{old['id']} -->", f"{MARKER_PREFIX}{new_id} -->")
            elif contrib["state"] == State.SUBMIT_FAILED.value:
                try:
                    ctx.state_machine.transition(
                        old["cid"], State.SUBMIT_FAILED, State.AWAITING_APPROVAL, Actor.HUMAN, {}, actor
                    )
                except TransitionError as exc:
                    raise ApprovalError("retry_unavailable", exc.code) from exc
            else:
                raise ApprovalError("retry_unavailable", contrib["state"])
            ctx.conn.execute(
                "INSERT INTO outbound_actions (id, cid, kind, payload_json, status, created_at) VALUES (?,?,?,?,?,?)",
                (new_id, old["cid"], old["kind"], json.dumps(payload, sort_keys=True), "pending_approval",
                 to_iso(ctx.clock.now())),
            )  # fmt: skip
            approval_id = ctx.create_approval(new_id, actor)
            ctx.audit.append(actor, "retry_requested", {"old_action": old["id"], "action_id": new_id}, old["cid"])
        ctx.on_approval_created(approval_id)
        await self._edit(query, "🔁 Retry requested: a fresh card follows (approve and confirm again).")
        await query.answer()

    # ------------------------------------------------------------------- messages
    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._timed("message", HANDLER_SLOW_S, self._on_message(update, context))

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or not authorized(update, self.ctx.telegram_chat_id):
            return
        text = (message.text or "").strip()
        if not text.startswith("/"):
            log.info("non-command text ignored (chat relay arrives with Hermes)", extra={"event": "chat_ignored"})
            return
        words = text.split()
        name = words[0][1:].split("@", 1)[0].lower()
        spec = COMMANDS.get(name)
        assert update.effective_user is not None
        cctx = CommandContext(self.ctx, update.effective_user.id, words[1:])
        if spec is None:
            reply = Reply(f"Unknown command {esc(words[0][:40])}. Try /help.")
        elif spec.effect == "io":  # BA-18: never hold the update slot on I/O
            self.spawn(self._run_command(message.chat.id, spec.func(cctx, words[1:]), context), "command")
            return
        else:
            try:
                reply = await spec.func(cctx, words[1:])
            except (Blocked, ApprovalError, TransitionError) as exc:
                reply = Reply(f"Refused: {esc(str(exc)[:300])}")
            if name in STOP_COMMANDS:
                # §14.18.5 item 4: the local effect (kill/pause transaction) is committed first, then
                # the cancellable work stops; submit and command tasks keep their Phase 1 rules.
                self.cancel(KILL_CANCELS)
        await self._send_reply(message.chat.id, reply, context)

    async def _run_command(self, chat_id: int, effect: Awaitable[Reply], context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            reply = await effect
        except (Blocked, ApprovalError, TransitionError) as exc:
            reply = Reply(f"Refused: {esc(str(exc)[:300])}")
        await self._send_reply(chat_id, reply, context)
