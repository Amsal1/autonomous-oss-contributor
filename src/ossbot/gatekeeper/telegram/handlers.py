"""Telegram update handlers: the deterministic decision path (DESIGN §14.2, §14.11.9, §12.4, I1, I3)
and the update routing table (§14.10.4, ``telegram/routing.py``).

* Every update passes ``auth.authorized`` first (owner, private chat); others get no reply.
* Callback data must be ``t:<22 chars>``; the token row must exist, have the right purpose, be
  bound to this message id, unused and unexpired. Rejections change nothing and log WARNING.
* Approve -> card edited to the mode-stating Confirm (LIVE / DRY RUN computed at that moment)
  -> Confirm -> live checks -> submit. Reject cancels the action. ``go`` commands confirm first.
  These U3 edits stay direct Bot API calls; every other reply and view goes through
  ``telegram.ui.send`` into the originating topic (§14.10.1).
* Kit buttons (navigation, pickers, comments, proposal and size controls) dispatch through
  ``telegram.actions.ACTIONS``; nothing typed in chat (or produced by Hermes) can reach a decision
  path, and ``/approve`` only re-sends a card (§14.18.4 delta 5).
* The update handler budget (§14.18.5): anything slower than SQLite, an outbox enqueue and one
  direct edit runs as a spawned task.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from datetime import timedelta
from typing import Any, Literal

from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, TypeHandler, filters

from ossbot.common.clock import from_iso, to_iso
from ossbot.common.control_flags import Blocked
from ossbot.common.ids import new_action_id
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalError
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.proposals import ProposalRefused, ProposalService
from ossbot.gatekeeper.state_machine import Actor, TransitionError
from ossbot.gatekeeper.telegram import (
    actions,
    cards,
    chat_media,
    chat_relay,
    implicit_topics,
    inputs,
    keyboards,
    proposal_cards,
    rawapi,
    routing,
)
from ossbot.gatekeeper.telegram.auth import authorized, authorized_chat_only
from ossbot.gatekeeper.telegram.commands import (
    builtin,  # noqa: F401 - registers the built-ins
    load_builtin_modules,
)
from ossbot.gatekeeper.telegram.commands.common import cancel_button
from ossbot.gatekeeper.telegram.commands.lifecycle import killed_view
from ossbot.gatekeeper.telegram.commands.registry import (
    COMMANDS,
    CONFIRM_EFFECTS,
    CONFIRMS,
    CommandContext,
    Reply,
)
from ossbot.gatekeeper.telegram.escape import b, esc
from ossbot.gatekeeper.telegram.review import confirm as review_confirm
from ossbot.gatekeeper.telegram.runtime import ui_of
from ossbot.gatekeeper.telegram.topics import CommandTarget
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import redact as ui_redact
from ossbot.gatekeeper.telegram.ui import send as ui_send
from ossbot.gatekeeper.telegram.ui.icons import SYSTEM_TOPICS
from ossbot.gatekeeper.telegram.ui.sanitize import undefuse as sanitize_undefuse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

load_builtin_modules()

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
# Message keys that are not its content type: ``tg_thread_fields.content_keys`` lists the rest by name.
ENVELOPE_KEYS = frozenset({"message_id", "date", "chat", "from", "sender_chat", "message_thread_id",
                           "is_topic_message", "reply_to_message", "quote", "forward_origin", "edit_date",
                           "entities", "caption_entities", "link_preview_options", "has_protected_content",
                           "is_from_offline", "author_signature", "via_bot", "reply_markup", "media_group_id",
                           "sender_boost_count", "sender_business_bot", "business_connection_id",
                           "external_reply", "is_automatic_forward", "show_caption_above_media"})  # fmt: skip
STOP_COMMANDS = frozenset({"kill", "pause"})
# AC 22: with a kill active, only these commands run; the rest get the killed message (§14.16).
KILLED_ALLOWED = frozenset({"status", "resume", "logs", "help", "start", "kill", "pause", "dryrun"})
KILLED_CONFIRMS = frozenset({"resume_confirm"})  # the only command confirm that acts while killed
KILLED_TEXT = "🛑 ossbot is killed: only /status /resume /logs /help work now. /resume to continue."
REJECT_REASONS = {"not_valuable": "Not valuable", "wrong_approach": "Wrong approach",
                  "repo_unsuitable": "Repo unsuitable (7d)", "other": "Other…", "back": "↩️ Back"}  # fmt: skip
REPO_UNSUITABLE_COOLDOWN = timedelta(days=7)
# Gate 1 decision purposes (p_ ids); the kit purposes (skip, adjust, ...) arrive with FEAT-009.
PROPOSAL_PURPOSES = frozenset({"proposal_start", "proposal_confirm", "proposal_cancel"})
# Skill activation (§14.15, §15.5; s_ ids): two taps; the kit card and the diff/ask views are PLAN item 47.
SKILL_PURPOSES = frozenset({"skill_approve", "skill_confirm", "skill_cancel", "skill_reject"})
SKILL_VIEWS = frozenset({"skill_diff", "skill_ask"})  # multi-use views of the skill card (§14.15)
ASK_WHY = "Explain why you want skill {skill} v{version} and what it will do"


def _button(btn: keyboards.Button) -> InlineKeyboardButton:
    style = btn[2] if len(btn) == 3 else None
    if btn[1].startswith(keyboards.MIRROR_URL_PREFIX):  # card row 4 (§14.11.14)
        return InlineKeyboardButton(btn[0], url=btn[1])
    return InlineKeyboardButton(btn[0], callback_data=keyboards.callback_data(btn[1]), style=style)


def markup(rows: keyboards.Rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_button(btn) for btn in row] for row in rows])


class Handlers:
    KILL_CANCELS = KILL_CANCELS

    def __init__(self, ctx: AppContext, submit: Submit | None = None, preflight: Preflight | None = None) -> None:
        self.ctx = ctx
        self.submit = submit
        self.preflight = preflight
        self.tasks: set[asyncio.Task[Any]] = set()
        self._by_kind: dict[TaskKind, set[asyncio.Task[Any]]] = {}
        self._sessions: dict[str, asyncio.Task[Any]] = {}  # session_key -> its running chat_turn
        self._stops: dict[int, asyncio.Event] = {}  # turn_id (= draft_id) -> its Stop event
        self.albums: dict[str, Any] = {}  # media_group_id -> its buffer (chat_media.Album)
        self.application: Application[Any, Any, Any, Any, Any, Any] | None = None
        # the gatekeeper's GitHub reader (GET only): /work and link-card facts, always in a github_read task
        self.reader: Any = None
        self.implicit = implicit_topics.ImplicitTopics()  # topic created for a command typed in All

    def register(self, application: Application[Any, Any, Any, Any, Any, Any]) -> None:
        self.application = application
        application.add_handler(CallbackQueryHandler(self.on_callback))
        application.add_handler(MessageHandler(filters.ALL, self.on_message))
        application.add_handler(TypeHandler(Update, self.on_raw), group=1)  # 10.3 updates PTB doesn't type

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

    def stop_event(self, turn_id: int) -> asyncio.Event:
        """The Stop signal of one streaming turn (``draft_id = turn_id``, §14.12.1 step 5)."""
        return self._stops.setdefault(turn_id, asyncio.Event())

    def forget_stop(self, turn_id: int) -> None:
        self._stops.pop(turn_id, None)

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
        inflight = getattr(self.ctx.telegraph, "inflight", None)  # F6-4: a shielded Telegraph request finishes
        while inflight:  # and its page row is written before the gatekeeper stops (§14.12.8 step 4)
            await asyncio.gather(*list(inflight), return_exceptions=True)

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
        if row is not None and str(row["approval_id"] or "").startswith("p_") and self._approval_status(row) == (
                "expired"):  # fmt: skip
            return proposal_cards.EXPIRED  # §14.11.15 Expiry
        if exc.code == "superseded" or (row is not None and self._approval_status(row) == "invalidated"):
            return "Superseded: content or mode changed since this card; review the new card."
        if exc.code in {"token_expired", "approval_expired"}:
            return "This button has expired."
        if exc.code in {"approve_refused", "confirm_refused", "blocked"}:
            detail = str(exc).split(": ", 1)[-1]
            verb = "approved" if exc.code == "approve_refused" else "confirmed"
            if detail == "paused or killed" or exc.code == "blocked":
                return f"Paused or killed: nothing was {verb}. /resume first."
            what = "down (workers)" if detail == "down" else detail[:20]
            return f"ossbot is {what}: nothing was {verb}. The card stays valid; retry after up."
        return "This button is no longer valid."

    def _approval_status(self, row: Any) -> str | None:
        if row["approval_id"] is None:
            return None
        aid = str(row["approval_id"])
        table = {"p_": "proposal_approvals", "s_": "skill_approvals"}.get(aid[:2], "approvals")
        appr = db.one(self.ctx.conn, f"SELECT status FROM {table} WHERE id = ?", (aid,))  # noqa: S608 - fixed names
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

    def reply_view(self, reply: Reply, *, stale_s: int = 0) -> k.View:
        """A command ``Reply`` as a kit view: its text is gatekeeper HTML; the confirm is a single-use,
        5-minute, message-bound token minted then bound at send (§14.18.2)."""
        stale: list[k.Line] = []
        if stale_s > 0:  # CR3-AC-16: the age is shown on whatever the stale command produced
            when = int(self.ctx.clock.now().timestamp()) - stale_s
            stale.append((k.Const("(sent "), k.Time(when, f"{stale_s // 60} min ago"),
                          k.Const(" while I was offline)")))  # fmt: skip
        if reply.view is not None:
            view: k.View = reply.view
            if stale:
                view.blocks = [*view.blocks, k.Block((k.Lines(tuple(stale)),))]
        else:
            lines: list[k.Line] = [(k.Html(reply.text),), *stale]
            kind = "confirm_prompt" if reply.confirm is not None else "fyi"
            view = k.View(kind=kind, blocks=[k.Block((k.Lines(tuple(lines)),))])
        if reply.confirm is not None:  # §14.14.2: [✅ <verb>] [↩️ Cancel]
            view.rows = [*view.rows, [k.ButtonSpec(reply.confirm.label, reply.confirm.purpose, single_use=True,
                                                   ttl=COMMAND_CONFIRM_TTL, raw_arg=reply.confirm.arg,
                                                   style=reply.confirm.style), cancel_button()]]  # fmt: skip
        return view

    def _send_reply(self, reply: Reply, *, thread_id: int | None = None, reply_to: int | None = None,
                    stale_s: int = 0) -> None:  # fmt: skip
        """Command replies land in the originating topic (§14.10.1)."""
        if reply.deferred:
            return
        ui_send.deliver(self.ctx, self.reply_view(reply, stale_s=stale_s), thread_id=thread_id, reply_to=reply_to)
        if reply.document is not None:
            name, data = reply.document
            ui_send.document(self.ctx, name, data, thread_id=thread_id)

    def _edit_reply(self, message_id: int, reply: Reply) -> None:
        """Edit a command reply or confirm panel in place (not an approval card: those are U3)."""
        ui_send.edit(self.ctx, self.reply_view(reply), message_id=message_id)

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
                rows = self.ctx.approvals.card_rows(row["approval_id"], tokens)
                await self._edit(query, self._card_text(row["approval_id"]), rows)
                await query.answer("Cancelled; nothing was submitted.")
            elif purpose == "reject":
                await self._reject_reasons(query, token, row, chat_id, message_id)
            elif purpose == "reject_reason":
                await self._reject_with_reason(query, token, row, user_id, chat_id, message_id)
            elif purpose == "retry":
                await self._retry(query, token, row, user_id, chat_id, message_id)
            elif purpose in PROPOSAL_PURPOSES:
                await self._proposal(query, token, purpose, row, user_id, chat_id, message_id)
            elif purpose in SKILL_PURPOSES:
                await self._skill(query, token, purpose, row, user_id, chat_id, message_id)
            elif purpose in SKILL_VIEWS:
                await self._skill_view(query, token, purpose, row, user_id, chat_id, message_id)
            elif purpose in CONFIRMS:
                with db.transaction(self.ctx.conn):
                    self.ctx.approvals.use_token(token, (purpose,), chat_id=chat_id, message_id=message_id)
                if purpose not in KILLED_CONFIRMS and self.ctx.control.view().state == "killed":
                    # §14.16: a confirm minted before /kill does nothing after it (token spent)
                    self._edit_reply(message_id, Reply(KILLED_TEXT, view=killed_view(self.ctx)))
                    await query.answer("Killed: nothing changed. /resume first.")
                    return
                thread = getattr(message, "message_thread_id", None)
                cctx = CommandContext(self.ctx, user_id, thread_id=thread, message_id=message_id, handlers=self)
                effect = CONFIRMS[purpose](cctx, row["arg"])
                if CONFIRM_EFFECTS.get(purpose, "local") == "io":  # BA-18: token used, ⏳ inline, effect spawned
                    self._edit_reply(message_id, Reply("⏳ Working…"))
                    await query.answer()
                    self.spawn(self._finish_confirm(message_id, effect), "command")
                else:
                    reply = await effect
                    self._edit_reply(message_id, reply)
                    await query.answer()
            elif purpose in actions.ACTIONS:
                await self._action(query, token, purpose, row, user_id, chat_id, message_id)
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
            text, rows = proposal_cards.confirm_panel(self.ctx, pid, arm.confirm_token, arm.cancel_token)
            await self._edit(query, text, rows)
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
            # danger: activation widens what Hermes does (§14.14.2)
            rows: keyboards.Rows = [
                [("🧠 Activate skill", arm.confirm_token, "danger"), ("↩️ Cancel", arm.cancel_token)]
            ]
            await self._edit(query, text, rows)
        elif purpose == "skill_confirm":
            svc.confirm(token, **tap)
            binding = self.ctx.skill_reviews.request_activation(sid, f"tg:{user_id}")
            done = b(f"Activating · v{binding.get('version')}")
            await self._edit(
                query, f"{svc.card_view(sid).card}\n\n🧠 {done}: root installs exactly the approved files."
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

    async def _skill_view(
        self, query: Any, token: str, purpose: str, row: Any, user_id: int, chat_id: int, message_id: int
    ) -> None:
        """🧾 Full diff (a redacted ``.diff`` document) and 💬 Ask Hermes why (relayed to the 💬 Assistant
        session; the 🤖 answer never touches the card)."""
        with db.transaction(self.ctx.conn):
            self.ctx.approvals.use_token(token, (purpose,), chat_id=chat_id, message_id=message_id)
        appr = db.one(self.ctx.conn, "SELECT s.skill, s.version, r.diff FROM skill_approvals s JOIN skill_reviews r"
                      " ON r.id = s.skill_review_id WHERE s.id = ?", (row["approval_id"],))  # fmt: skip
        if appr is None:
            await query.answer("This skill is no longer staged.")
            return
        if purpose == "skill_diff":
            diff = ui_redact.redact(str(appr["diff"] or ""), surface="skill_diff")[0] or "(no textual change)"
            ui_send.document(self.ctx, f"{appr['skill']}-v{appr['version']}.diff", diff.encode("utf-8"),
                             thread="reviews", reply_to=message_id)  # fmt: skip
            await query.answer("🧾 Full diff below.")
            return
        text = f"Re: skill {appr['skill']} v{appr['version']}\n" + ASK_WHY.format(skill=appr["skill"],
                                                                                  version=appr["version"])  # fmt: skip
        await chat_relay.relay(self.ctx, self, kind="system_chat", topic="assistant", text=text, user_id=user_id,
                               thread_id=None, message_id=message_id)  # fmt: skip
        await query.answer("💬 Asked Hermes in 💬 Assistant.")

    async def _finish_confirm(self, message_id: int, effect: Awaitable[Reply]) -> None:
        try:
            reply = await effect
        except (Blocked, ApprovalError, TransitionError) as exc:
            reply = Reply(f"Refused: {esc(str(exc)[:300])}")
        self._edit_reply(message_id, reply)

    async def _action(
        self, query: Any, token: str, purpose: str, row: Any, user_id: int, chat_id: int, message_id: int
    ) -> None:
        """A kit button (§14.18.2): token rules first, then the purpose's arg model, then the handler."""
        spec = actions.ACTIONS[purpose]
        with db.transaction(self.ctx.conn):
            tok = self.ctx.approvals.use_token(token, (purpose,), chat_id=chat_id, message_id=message_id)
        try:
            arg = actions.parse_arg(spec, tok["arg"])
        except actions.BadArg:
            log.warning("callback rejected: bad arg for %s", purpose, extra={"event": "callback_rejected"})
            await query.answer("This button is no longer valid.")
            return
        message = query.message
        tap = actions.Tap(self.ctx, self, token, tok, arg, user_id, chat_id, message_id,
                          getattr(message, "message_thread_id", None))  # fmt: skip
        if spec.effect == "io":
            await query.answer("⏳")
            self.spawn(self._run_action(spec, tap), "command")
            return
        try:
            text = await spec.func(tap)
        except (Blocked, ApprovalError, TransitionError) as exc:
            text = f"Refused: {str(exc)[:150]}"
        await query.answer(k.toast(text))

    async def _run_action(self, spec: actions.ActionSpec, tap: actions.Tap) -> None:
        try:
            text = await spec.func(tap)
        except (Blocked, ApprovalError, TransitionError) as exc:
            text = f"Refused: {str(exc)[:300]}"
        if text:
            ui_send.deliver(self.ctx, k.plain("fyi", (k.Const(text),)), thread_id=tap.thread_id)

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
        text = self.confirm_panel(arm.approval_id, arm.confirm_text, arm.live)
        await self._edit(query, text, keyboards.confirm_rows(arm.confirm_token, arm.cancel_token, live=arm.live))
        await query.answer()

    def confirm_panel(self, approval_id: str, sentence: str, live: bool) -> str:
        """S8 (§14.11.9): the card's head, the mode line, the exact confirm sentence, the coverage line,
        the unsent-comments warning and the expiry."""
        row = db.one(self.ctx.conn, "SELECT o.kind, o.cid, o.payload_json, o.id FROM approvals a JOIN"
                     " outbound_actions o ON o.id = a.action_id WHERE a.id = ?", (approval_id,))  # fmt: skip
        if row is None:
            return b(sentence)
        rev = json.loads(row["payload_json"]).get("rev")
        return review_confirm.panel(
            self._card_text(approval_id), kind=str(row["kind"]), live=live, sentence=sentence, cid=str(row["cid"]),
            rev=rev if isinstance(rev, int) else None, files=cards.file_count(self.ctx.conn, str(row["id"])),
            conn=self.ctx.conn, now=self.ctx.clock.now(),
            confirm_ttl_s=self.ctx.config.policy.limits.confirm_ttl_seconds,
        )  # fmt: skip

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
            result = await self.submit(approval_id)
            self._submitted(approval_id, str(result))
        except ApprovalError as exc:
            log.warning("submit refused: %s", exc.code, extra={"event": "submit_refused"})
            self.ctx.notify(f"Submission refused ({esc(exc.code)}); nothing was published.")
        except Exception:
            log.exception("submission crashed", extra={"event": "submit_crashed"})
            self.ctx.notify(
                "🚨 Submission crashed; restart reconciliation will resolve it. Nothing is retried automatically."
            )

    def _submitted(self, approval_id: str, result: str) -> None:
        """S9 (§14.11.10): the card is finalized: LIVE ``🚀 Submitted · PR #N`` with the PR link and a copy
        button, or ``🧪 Dry run done — nothing was published``."""
        row = db.one(self.ctx.conn, "SELECT a.tg_chat_id, a.tg_message_id, o.cid, o.status, c.pr_number, c.pr_url"
                     " FROM approvals a JOIN outbound_actions o ON o.id = a.action_id JOIN contributions c"
                     " ON c.id = o.cid WHERE a.id = ?", (approval_id,))  # fmt: skip
        if row is None or row["tg_message_id"] is None:
            return
        card = self._card_text(approval_id)
        markup: dict[str, Any] | None = None
        if row["status"] == "dry_run_done" or result == "dry_run_done":
            text = f"{card}\n\n🧪 {b('Dry run done — nothing was published')}"
        elif row["status"] == "done":
            pr = f" · PR #{row['pr_number']}" if row["pr_number"] else ""
            text = f"{card}\n\n🚀 {b('Submitted' + pr)}"
            url = str(row["pr_url"] or "")
            if url.startswith("https://github.com/"):
                markup = {"inline_keyboard": [[{"text": "🔗 Open PR ↗", "url": url},
                                               {"text": "📋 Copy link", "copy_text": {"text": url}}]]}  # fmt: skip
        else:
            return
        payload: dict[str, Any] = {"chat_id": row["tg_chat_id"], "message_id": row["tg_message_id"], "text": text,
                                   "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}  # fmt: skip
        if markup is not None:
            payload["reply_markup"] = markup
        self.ctx.outbox.enqueue("editMessageText", payload, dedupe_key=f"stub:{row['tg_message_id']}:submitted",
                                thread_key="reviews")  # fmt: skip

    async def _reject(self, query: Any, token: str, user_id: int, chat_id: int, message_id: int, *,
                      reason: str | None = None) -> None:  # fmt: skip
        with db.transaction(self.ctx.conn):
            action_id = self.ctx.approvals.reject(token, tg_user_id=user_id, chat_id=chat_id, message_id=message_id,
                                                  reason=reason)  # fmt: skip
            self.rejected(action_id, f"tg:{user_id}")
        await self._edit(query, f"{self._card_text_safe(action_id)}\n\n❌ {b('Rejected')}; nothing was published.")
        await query.answer("Rejected.")

    def rejected(self, action_id: str, actor: str) -> None:
        """The effects of a rejected approval (card ❌ or ``/reject``); caller holds the transaction."""
        ctx = self.ctx
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

    def rejected_stub(self, approval_id: str, action_id: str) -> None:
        """``/reject``: the card on screen becomes ``❌ Rejected`` without buttons (its tokens are dead)."""
        row = db.one(self.ctx.conn, "SELECT tg_chat_id, tg_message_id FROM approvals WHERE id = ?", (approval_id,))
        if row is None or row["tg_message_id"] is None:
            return
        text = f"{self._card_text_safe(action_id)}\n\n❌ {b('Rejected')}; nothing was published."
        payload: dict[str, Any] = {"chat_id": row["tg_chat_id"], "message_id": row["tg_message_id"], "text": text,
                                   "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}  # fmt: skip
        self.ctx.outbox.enqueue("editMessageText", payload, dedupe_key=f"stub:{row['tg_message_id']}:rejected",
                                thread_key="reviews")  # fmt: skip

    def repo_cooldown(self, cid: str, actor: str) -> None:
        """``Repo unsuitable (7d)``: no new proposals for the contribution's repo for 7 days."""
        contrib = db.one(self.ctx.conn, "SELECT upstream FROM contributions WHERE id = ?", (cid,))
        if contrib is None:
            return
        until = to_iso(self.ctx.clock.now() + REPO_UNSUITABLE_COOLDOWN)
        with db.transaction(self.ctx.conn):
            self.ctx.conn.execute(
                "INSERT INTO repo_overrides (upstream, cooldown_until, updated_by, updated_at) VALUES (?,?,?,?)"
                " ON CONFLICT(upstream) DO UPDATE SET cooldown_until = excluded.cooldown_until,"
                " updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                (contrib["upstream"], until, actor, to_iso(self.ctx.clock.now())),
            )  # fmt: skip

    def ask_reject_reason(self, cid: str, approval_id: str, reply_to: int | None) -> None:
        """``Other…``: a force-reply prompt for the free-text reason (optional, ≤ 500 chars)."""
        view = k.plain("confirm_prompt", k.line("Why? Reply with the reason (≤ 500 chars, optional)."))
        view.placeholder = "Why was it rejected? (optional)"
        view.state = {"prompt": {"kind": "reject_other", "cid": cid, "approval_id": approval_id}}
        ui_send.deliver(self.ctx, view, thread="reviews", reply_to=reply_to)

    async def _reject_reasons(self, query: Any, token: str, row: Any, chat_id: int, message_id: int) -> None:
        """❌ Reject: the card's keyboard becomes the reason row (FEAT-009 item 45); each reason is a
        single-use ``reject_reason`` token of this approval on this message (U3), and ↩️ Back restores S1."""
        approval_id = str(row["approval_id"])
        with db.transaction(self.ctx.conn):
            self.ctx.approvals.use_token(token, ("reject",), chat_id=chat_id, message_id=message_id)
            appr = db.one(self.ctx.conn, "SELECT status, expires_at FROM approvals WHERE id = ?", (approval_id,))
            if appr is None or appr["status"] != "pending":
                raise ApprovalError("wrong_status", str(appr["status"]) if appr else "unknown")
            mint = {"chat_id": chat_id, "message_id": message_id, "expires_at": from_iso(appr["expires_at"]),
                    "approval_id": approval_id, "cid": row["cid"]}  # fmt: skip
            tokens = {r: self.ctx.approvals.mint_token("reject_reason", arg=r, **mint) for r in REJECT_REASONS}
        rows: keyboards.Rows = [
            [(REJECT_REASONS["not_valuable"], tokens["not_valuable"]),
             (REJECT_REASONS["wrong_approach"], tokens["wrong_approach"])],
            [(REJECT_REASONS["repo_unsuitable"], tokens["repo_unsuitable"]),
             (REJECT_REASONS["other"], tokens["other"])],
            [(REJECT_REASONS["back"], tokens["back"])],
        ]  # fmt: skip
        await self._edit(query, f"{self._card_text(approval_id)}\n\n❌ {b('Reject: why?')}", rows)
        await query.answer()

    async def _reject_with_reason(self, query: Any, token: str, row: Any, user_id: int, chat_id: int,
                                  message_id: int) -> None:  # fmt: skip
        approval_id = str(row["approval_id"])
        reason = str(row["arg"] or "")
        if reason == "back":
            self.ctx.approvals.check_token(token, ("reject_reason",), chat_id=chat_id, message_id=message_id)
            tokens = self.ctx.approvals.rotate_card_tokens(approval_id)
            await self._edit(query, self._card_text(approval_id), self.ctx.approvals.card_rows(approval_id, tokens))
            await query.answer()
            return
        await self._reject(query, token, user_id, chat_id, message_id, reason=reason)
        if reason == "repo_unsuitable":
            self.repo_cooldown(str(row["cid"]), f"tg:{user_id}")
        elif reason == "other":
            self.ask_reject_reason(str(row["cid"]), approval_id, message_id)

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

    def _incoming(self, message: Any) -> routing.Incoming:
        reply_to = message.reply_to_message
        urls = tuple(
            str(e.url) if e.type == "text_link" and e.url else message.parse_entity(e)
            for e in (message.entities or ())
            if e.type in {"url", "text_link"}
        )
        has_media = bool(message.photo or message.document or message.voice or message.audio or message.video
                         or message.location)  # fmt: skip
        created = reply_to.forum_topic_created if reply_to is not None else None
        return routing.Incoming(
            kind="message", chat_id=message.chat.id, text=message.text or message.caption or "",
            thread_id=message.message_thread_id if message.is_topic_message else None,
            reply_to_message_id=reply_to.message_id if reply_to is not None else None,
            reply_to_from_bot=bool(reply_to is not None and reply_to.from_user and reply_to.from_user.is_bot),
            forwarded=message.forward_origin is not None, entity_urls=urls, has_media=has_media,
            date=int(message.date.timestamp()) if message.date else 0,
            raw_thread_id=message.message_thread_id, is_topic_message=bool(message.is_topic_message),
            implicit_topic=bool(created is not None and created.is_name_implicit),
        )  # fmt: skip

    @staticmethod
    def _log_thread_fields(message: Any, inc: routing.Incoming) -> None:
        """Diagnostic (ADR-0020 addendum 2026-10-21): the thread facts of an owner message. Ids and
        flags only: never the text, the chat id or the user id."""
        reply_to = message.reply_to_message
        created = reply_to.forum_topic_created if reply_to is not None else None
        own = message.forum_topic_created  # update A of a command typed in All (ADR-0020 addendum)
        sender = message.from_user
        fields: dict[str, Any] = {
            "message_id": message.message_id, "message_thread_id": inc.raw_thread_id,
            "is_topic_message": inc.is_topic_message, "reply_topic_created": created is not None,
            "is_name_implicit": created.is_name_implicit if created is not None else None,
            "has_direct_messages_topic": getattr(message, "direct_messages_topic", None) is not None,
            "is_command": inc.text.lstrip().startswith("/"),
            "unknown_keys": sorted(str(key) for key in (message.api_kwargs or {})),
            # which content or service field is set: key names only, never a value
            "content_keys": sorted(str(key) for key, value in message.to_dict().items()
                                   if key not in ENVELOPE_KEYS and value not in (None, False)),
            "from_is_bot": bool(sender is not None and sender.is_bot),
            "from_is_owner": bool(sender is not None and sender.id == message.chat.id),
            "topic_created": None if own is None else {
                "name_len": len(own.name or ""), "name_is_command": (own.name or "").lstrip().startswith("/"),
                "icon_color": own.icon_color, "has_icon_custom_emoji": own.icon_custom_emoji_id is not None,
                "is_name_implicit": own.is_name_implicit,
            },
        }  # fmt: skip
        log.info("tg_thread_fields message=%s thread=%s topic_message=%s implicit=%s content=%s",
                 fields["message_id"], inc.raw_thread_id if inc.raw_thread_id is not None else "none",
                 inc.is_topic_message, fields["is_name_implicit"], ",".join(fields["content_keys"]) or "none",
                 extra={"event": "tg_thread_fields", **fields})  # fmt: skip

    def _count_message(self, inc: routing.Incoming) -> None:
        """§14.11.3: owner messages count toward the thread's message counter (file-view reuse). A thread
        id without ``is_topic_message`` is counted under its raw key too (``command_target``)."""
        keys = [f"{inc.chat_id}:{inc.thread_id or 0}"]
        if inc.raw_thread_id is not None and inc.raw_thread_id != inc.thread_id:
            keys.append(f"{inc.chat_id}:{inc.raw_thread_id}")
        with db.transaction(self.ctx.conn):
            for key in keys:
                self.ctx.conn.execute("INSERT INTO tg_thread_seq (thread_key, n) VALUES (?, 1) ON CONFLICT"
                                      "(thread_key) DO UPDATE SET n = n + 1", (key,))  # fmt: skip

    def _thread_seq(self, chat_id: int, thread_id: int | None) -> int:
        if thread_id is None:
            return 0
        row = db.one(self.ctx.conn, "SELECT n FROM tg_thread_seq WHERE thread_key = ?", (f"{chat_id}:{thread_id}",))
        return int(row["n"]) if row is not None else 0

    def _stale_s(self, inc: routing.Incoming) -> int:
        """Seconds the message waited, when that is over ``telegram.stale_message_minutes`` (else 0)."""
        if inc.date <= 0:
            return 0
        waited = int(self.ctx.clock.now().timestamp()) - inc.date
        return waited if waited > self.ctx.config.telegram.stale_message_minutes * 60 else 0

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or not authorized(update, self.ctx.telegram_chat_id):
            return
        assert update.effective_user is not None
        user_id = update.effective_user.id
        inc = self._incoming(message)
        self._log_thread_fields(message, inc)
        ui = ui_of(self.ctx)
        route = routing.classify(self.ctx.conn, ui.topics, ui.pending, inc)
        stale_s = self._stale_s(inc)
        quote = message.quote.text if message.quote is not None else None
        doc = message.document
        typed = inputs.Typed(
            self.ctx,
            self,
            sanitize_undefuse(inc.text),
            user_id,
            inc.chat_id,
            message.message_id,
            inc.thread_id,
            quote=quote,
            quote_manual=bool(message.quote is not None and message.quote.is_manual),
            document=(doc.file_id, doc.mime_type, doc.file_size or 0, doc.file_name) if doc is not None else None,
        )
        thread_seen = self._thread_seq(inc.chat_id, inc.raw_thread_id)  # 0 = a thread never seen before
        self._count_message(inc)
        if message.forum_topic_created is not None:
            self._on_topic_created(message, inc, user_id, thread_seen)
        if route.kind == "pending_input":
            prompt = ui.pending.consume(inc.chat_id, inc.reply_to_message_id)
            handler = inputs.INPUTS.get(prompt.kind) if prompt is not None else None
            if prompt is not None and handler is not None:
                typed.prompt = prompt
                await handler(typed)
            return
        if route.kind == "command":
            await self._command(inc, user_id, message.message_id, stale_s)
            return
        if route.kind == "view_reply" and route.view is not None:
            family = inputs.VIEW_FAMILY.get(str(route.view["kind"]))
            handler = inputs.VIEW_REPLIES.get(family) if family else None
            if handler is not None:
                typed.view = route.view
                await handler(typed)
                return
        if route.kind == "link_card":
            handler = inputs.VIEW_REPLIES.get("links")
            if handler is not None:
                typed.route = route
                await handler(typed)
            if route.then is None:
                return
        if route.kind == "ignore":
            return
        kind = route.then if route.kind == "link_card" and route.then else routing.chat_kind(route.topic)
        note = None
        if stale_s:
            note = (k.Const("↪️ replying to your message from "), k.Time(inc.date, f"{stale_s // 60} min ago"))
        text = typed.text
        if inc.has_media:  # §14.12.5: downloads and stripping run in a spawned media task, never here
            location = chat_media.location_text(message)
            if location is None:
                chat_media.accept(self.ctx, self, message, chat_media.Where(kind, route.topic, user_id, inc.thread_id,
                                                                            note))  # fmt: skip
                return
            text = location
        await chat_relay.relay(self.ctx, self, kind=kind, topic=route.topic, text=text, user_id=user_id,
                               thread_id=inc.thread_id, message_id=message.message_id, stale_note=note)  # fmt: skip

    async def _command(self, inc: routing.Incoming, user_id: int, message_id: int, stale_s: int) -> None:
        words = inc.text.strip().split()
        name = words[0][1:].split("@", 1)[0].lower()
        spec = COMMANDS.get(name)
        target = ui_of(self.ctx).topics.command_target(inc, self._thread_seq(inc.chat_id, inc.raw_thread_id))
        log.info("command_reply_target where=%s thread=%s delete=%s", target.where,
                 target.thread_id if target.thread_id is not None else "none",
                 target.delete_thread if target.delete_thread is not None else "none",
                 extra={"event": "command_reply_target", "where": target.where, "reply_thread": target.thread_id,
                        "delete_thread": target.delete_thread})  # fmt: skip
        thread_id = target.thread_id
        cctx = CommandContext(self.ctx, user_id, words[1:], thread_id=thread_id, message_id=message_id,
                              stale_s=stale_s, handlers=self)  # fmt: skip
        if spec is None:
            reply = Reply(f"Unknown command {esc(words[0][:40])}. Try /help.")
        elif self.ctx.control.view().state == "killed" and name not in KILLED_ALLOWED:
            reply = Reply(KILLED_TEXT, view=killed_view(self.ctx))  # §14.16: reason, since, Resume and Logs
        elif spec.effect == "io":  # BA-18: never hold the update slot on I/O
            after = functools.partial(self._after_command_reply, inc, user_id, message_id, target)
            self.spawn(self._run_command(spec.func(cctx, words[1:]), thread_id, stale_s, after), "command")
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
        self._send_reply(reply, thread_id=thread_id, stale_s=stale_s)
        self._after_command_reply(inc, user_id, message_id, target)

    async def _run_command(self, effect: Awaitable[Reply], thread_id: int | None, stale_s: int,
                           after: Callable[[], None] | None = None) -> None:  # fmt: skip
        try:
            reply = await effect
        except (Blocked, ApprovalError, TransitionError) as exc:
            reply = Reply(f"Refused: {esc(str(exc)[:300])}")
        self._send_reply(reply, thread_id=thread_id, stale_s=stale_s)
        if after is not None:
            after()

    def _after_command_reply(self, inc: routing.Incoming, user_id: int, message_id: int,
                             target: CommandTarget) -> None:  # fmt: skip
        """Runs once the reply is queued: the S1 implicit topic (flag on the command itself), or the
        update-A correlation of a command typed in All (``implicit_topics``)."""
        self._delete_stray_topic(target.delete_thread)
        if target.where != "all" or inc.raw_thread_id is not None or inc.is_topic_message:
            return
        match = self.implicit.on_command(implicit_topics.CommandSeen(inc.chat_id, user_id, message_id, inc.text,
                                                                     self._now_s()))  # fmt: skip
        if match is None:
            log.info("implicit_topic_pending side=command message=%s", message_id,
                     extra={"event": "implicit_topic_pending", "side": "command",
                            "message_id": message_id})  # fmt: skip
            return
        self._delete_implicit(match, inc.chat_id)

    def _on_topic_created(self, message: Any, inc: routing.Incoming, user_id: int, thread_seen: int) -> None:
        """Update A: a ``forum_topic_created`` service message. Only a thread the gatekeeper had never
        seen (no owner message, no bot message), and never a system topic, may pair with a command."""
        created = message.forum_topic_created
        thread = inc.raw_thread_id
        topics = ui_of(self.ctx).topics
        if thread is None or topics.mode == "flat":
            return
        reason = "system_topic" if self._is_system_thread(thread) else "known_thread" if thread_seen else None
        if reason is not None:
            log.info("implicit_topic_ignored thread=%s reason=%s", thread, reason,
                     extra={"event": "implicit_topic_ignored", "thread_id": thread, "reason": reason})  # fmt: skip
            return
        seen = implicit_topics.TopicSeen(inc.chat_id, user_id, message.message_id, thread, created.name or "",
                                         bool(created.is_name_implicit), self._now_s())  # fmt: skip
        match = self.implicit.on_topic(seen)
        if match is None:
            log.info("implicit_topic_pending side=topic message=%s thread=%s", message.message_id, thread,
                     extra={"event": "implicit_topic_pending", "side": "topic", "message_id": message.message_id,
                            "thread_id": thread})  # fmt: skip
            return
        self._delete_implicit(match, inc.chat_id)

    def _now_s(self) -> float:
        return self.ctx.clock.now().timestamp()

    def _is_system_thread(self, thread_id: int) -> bool:
        topics = ui_of(self.ctx).topics
        return thread_id in {topics.thread_id_raw(name) for name in SYSTEM_TOPICS}

    def _bot_posted_in(self, chat_id: int, thread_id: int) -> bool:
        row = db.one(self.ctx.conn, "SELECT 1 FROM tg_outbox WHERE method LIKE 'send%' AND"
                                    " json_extract(payload_json, '$.message_thread_id') = ? AND"
                                    " json_extract(payload_json, '$.chat_id') = ? LIMIT 1",
                     (thread_id, chat_id))  # fmt: skip
        return row is not None

    def _delete_implicit(self, match: implicit_topics.Match, chat_id: int) -> None:
        """A proven pair: delete the auto-created thread, re-checking it is no system topic and holds
        nothing the bot sent. The log carries ids and flags only (never the name or the text)."""
        thread = match.thread_id
        action = ("refused_system" if self._is_system_thread(thread)
                  else "refused_bot_posted" if self._bot_posted_in(chat_id, thread) else "delete")  # fmt: skip
        log.info("implicit_topic_match thread=%s order=%s gap_ms=%s name_matches_command=%s implicit=%s action=%s",
                 thread, match.order, match.gap_ms, match.name_matches_command, match.is_name_implicit, action,
                 extra={"event": "implicit_topic_match", "thread_id": thread, "order": match.order,
                        "gap_ms": match.gap_ms, "name_matches_command": match.name_matches_command,
                        "is_name_implicit": match.is_name_implicit, "action": action})  # fmt: skip
        if action == "delete":
            self._delete_stray_topic(thread)

    def _delete_stray_topic(self, thread_id: int | None) -> None:
        """Remove the topic Telegram created implicitly for a command typed in All (``deleteForumTopic``
        works in a private chat with a user, Bot API 9.3); queued after the reply, at most once per thread
        (outbox dedupe key), never retried on a permanent error (``TelegramFrontend._on_failed`` logs
        ``stray_topic_delete_failed``; private chats have no close or hide fallback)."""
        if thread_id is None or self.ctx.telegram_chat_id == 0:
            return
        req = rawapi.DeleteForumTopic(chat_id=self.ctx.telegram_chat_id, message_thread_id=thread_id)
        self.ctx.outbox.enqueue("deleteForumTopic", req.payload(), dedupe_key=f"topic_delete:{thread_id}")

    # ------------------------------------------------- stopped_message_generation (rule 2)
    async def on_raw(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Bot API 10.3 ``stopped_message_generation`` (from ``api_kwargs``; no ``from``, U10): cancel
        the owner's own stream whose ``draft_id`` is that turn's id. Unknown drafts are ignored."""
        stopped = rawapi.parse_stopped(update.api_kwargs)
        if stopped is None:
            return
        if not authorized_chat_only(stopped.chat.type, stopped.chat.id, self.ctx.telegram_chat_id):
            return
        row = db.one(self.ctx.conn, "SELECT session_key FROM chat_turns WHERE turn_id = ? AND status = 'streaming'",
                     (stopped.draft_id,))  # fmt: skip
        task = self._sessions.get(str(row["session_key"])) if row is not None else None
        stop = self._stops.get(stopped.draft_id)
        if task is None or stop is None:
            log.debug("stopped_message_generation for an unknown draft", extra={"event": "stop_ignored"})
            return
        stop.set()  # the turn task closes the stream and sends the partial with "⏹ Stopped by you"
