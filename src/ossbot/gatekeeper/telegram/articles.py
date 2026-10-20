"""Telegraph articles in Telegram (DESIGN §14.12.8; AC 27, 65, 68): ``📖 Open as article``, its confirm
panel, Unpublish and ``/articles``. Off by default: nothing here renders unless ``telegraph.eligible``.

Two taps: ``article_publish`` (bound to the answer message, 24 h) sends a classic confirm panel as a reply
to the answer; ``article_confirm`` (single-use, 5 min, bound to the panel) only checks the token and the
control state, edits the panel to ``⏳ Publishing…`` and spawns the rest as a ``telegraph`` task (a
``/kill`` cancels it). A task cancelled before its request leaves the panel at ``Not published
(stopped).``; one cancelled during the shielded ``createPage`` still records the page and edits the panel
to ``📖 Published (after stop) · public link · see /articles`` (one announcement either way).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from ossbot.gatekeeper import db
from ossbot.gatekeeper import telegraph as tgph
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.actions import Tap, action
from ossbot.gatekeeper.telegram.commands.registry import CommandContext, Reply, command
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send

CONFIRM_TTL = timedelta(minutes=5)
NA = "Telegraph is not GitHub"
PANEL_LINES = ("This publishes now, even in dry-run mode.",
               "Anyone with the link can read it; Telegraph can't delete pages — Unpublish only blanks it.",
               "Only secrets, email, phone and name are checked — read the answer for other personal details "
               "first.")  # fmt: skip
PATH_PATTERN = r"^[A-Za-z0-9-]{1,200}$"


class TurnArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: Annotated[int, Field(ge=1)]


class PathArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: Annotated[str, Field(pattern=PATH_PATTERN)]


def offer_rows(ctx: AppContext, turn_id: int) -> k.Rows:
    """The ``📖`` row of a ``chat_answer`` view, only for an eligible turn (checked at render)."""
    if ctx.telegraph is None or tgph.eligible(ctx, tgph.turn_row(ctx, turn_id)) is not None:
        return []
    return [[k.button("📖 Open as article", "article_publish", {"turn_id": turn_id})]]


def _panel(ctx: AppContext, turn_id: int) -> k.View:
    dry = bool(ctx.control.view().dry_run)
    head: k.Line = (k.Const("📖 "), k.Bold(k.Const("Publish as a public Telegraph page?")), k.Const(" · "),
                    k.mode_badge(dry, na=NA))  # fmt: skip
    rows: k.Rows = [[k.button("📖 Publish", "article_confirm", {"turn_id": turn_id}, style="danger", single_use=True,
                              ttl=CONFIRM_TTL),
                     k.button("↩️ Cancel", "article_cancel", single_use=True, ttl=CONFIRM_TTL)]]  # fmt: skip
    return k.View(kind="confirm_prompt", blocks=[k.Block((k.Lines((head, *(k.line(t) for t in PANEL_LINES))),))],
                  rows=rows)  # fmt: skip


def _say(ctx: AppContext, message_id: int, text: str) -> None:
    ui_send.edit(ctx, k.plain("fyi", k.line(text)), message_id=message_id)


def _published_view(page: tgph.Page, title: str, *, path: str) -> k.View:
    head: k.Line = (k.Const("📖 "), k.Bold(k.Const("Published")), k.Const(" · "), k.Text(title, 120),
                    k.Const(" · public link, not deletable (only blanked)"))  # fmt: skip
    rows: k.Rows = [[k.url_button("📖 Open ↗", page.url), k.copy_button("📋 Copy link", page.url)],
                    [k.button("🗑 Unpublish", "article_unpublish", {"path": path})]]  # fmt: skip
    return k.View(kind="fyi", blocks=[k.Block((k.Lines((head,)),))], rows=rows, link_preview=page.url)


@action("article_publish", arg=TurnArg)
async def article_publish(tap: Tap) -> str:
    app = tap.app
    if app.telegraph is None:
        return "Telegraph is off."
    if not tgph.system_running(app):
        return tgph.REFUSED_STATE
    why = tgph.eligible(app, tgph.turn_row(app, tap.arg.turn_id))
    if why is not None:
        return f"Not eligible: {why}"
    if db.one(app.conn, "SELECT 1 FROM telegraph_pages WHERE turn_id = ? AND unpublished_at IS NULL",
              (tap.arg.turn_id,)) is not None:  # fmt: skip
        return "Already published · see /articles"
    ui_send.deliver(app, _panel(app, tap.arg.turn_id), thread_id=tap.thread_id, reply_to=tap.message_id)
    return ""


@action("article_cancel")
async def article_cancel(tap: Tap) -> str:
    _say(tap.app, tap.message_id, "Not published.")
    return ""


@action("article_confirm", arg=TurnArg)
async def article_confirm(tap: Tap) -> str:
    app = tap.app
    if app.telegraph is None:
        _say(app, tap.message_id, "Not published.")
        return "Telegraph is off."
    if not tgph.system_running(app):
        _say(app, tap.message_id, "Not published (paused or killed).")
        return tgph.REFUSED_STATE
    if tgph.eligible(app, tgph.turn_row(app, tap.arg.turn_id)) is not None:
        _say(app, tap.message_id, tgph.NOT_ELIGIBLE)
        return tgph.NOT_ELIGIBLE
    _say(app, tap.message_id, "⏳ Publishing…")
    run = tgph.Publishing()
    panel, answer, thread = tap.message_id, _answer_of(app, tap), tap.thread_id

    def announce(page: tgph.Page, title: str, after_stop: bool) -> None:
        if after_stop:
            _say(app, panel, "📖 Published (after stop) · public link · see /articles")
            return
        _say(app, panel, "📖 Published ↓")
        ui_send.deliver(app, _published_view(page, title, path=page.path), thread_id=thread, reply_to=answer)

    async def work() -> None:
        try:
            status, result = await app.telegraph.publish(tap.arg.turn_id, run, announce)
        except asyncio.CancelledError:
            run.cancelled = True
            raise
        except tgph.TelegraphError as exc:
            run.phase = "done"
            _say(app, panel, f"Not published: Telegraph error ({str(exc)[:80]})")
            return
        if status == "refused":
            run.phase = "done"
            _say(app, panel, str(result))

    def on_done(task: asyncio.Task[Any]) -> None:
        if task.cancelled() and run.phase == "before":
            _say(app, panel, "Not published (stopped).")

    tap.handlers.spawn(work(), "telegraph", on_done=on_done)
    return ""


def _answer_of(app: AppContext, tap: Tap) -> int | None:
    """The answer message the panel replies to (the panel was sent as its reply)."""
    row = db.one(app.conn, "SELECT payload_json FROM tg_outbox WHERE result_message_id = ? ORDER BY id DESC LIMIT 1",
                 (tap.message_id,))  # fmt: skip
    if row is None:
        return None
    import json  # noqa: PLC0415 - small

    reply = (json.loads(row["payload_json"]).get("reply_parameters") or {}).get("message_id")
    return int(reply) if isinstance(reply, int) else None


@action("article_unpublish", arg=PathArg)
async def article_unpublish(tap: Tap) -> str:
    app = tap.app
    if not tgph.system_running(app):
        return tgph.REFUSED_UNPUBLISH
    rows: k.Rows = [[k.button("🗑 Unpublish", "article_unpublish_confirm", {"path": tap.arg.path}, style="danger",
                              single_use=True, ttl=CONFIRM_TTL),
                     k.button("↩️ Cancel", "article_cancel", single_use=True, ttl=CONFIRM_TTL)]]  # fmt: skip
    view = k.confirm_view("Unpublish this page? Telegraph can only blank it: cached copies, Instant View caches and"
                          " anyone who opened it may still have the content.", rows=rows)  # fmt: skip
    ui_send.deliver(app, view, thread_id=tap.thread_id, reply_to=tap.message_id)
    return ""


@action("article_unpublish_confirm", arg=PathArg)
async def article_unpublish_confirm(tap: Tap) -> str:
    app = tap.app
    if app.telegraph is None:
        return "Telegraph is off."
    if not tgph.system_running(app):
        _say(app, tap.message_id, tgph.REFUSED_UNPUBLISH)
        return tgph.REFUSED_UNPUBLISH
    _say(app, tap.message_id, "⏳ Unpublishing…")
    run = tgph.Publishing()
    panel = tap.message_id

    def announce(after_stop: bool) -> None:
        _say(
            app,
            panel,
            "🗑 Unpublished (after stop)"
            if after_stop
            else "🗑 Unpublished. Cached copies, Instant View caches and anyone who opened it may still have it.",
        )

    async def work() -> None:
        try:
            text = await app.telegraph.unpublish(tap.arg.path, run, announce)
        except asyncio.CancelledError:
            run.cancelled = True
            raise
        except tgph.TelegraphError as exc:
            run.phase = "done"
            _say(app, panel, f"Not changed: Telegraph error ({str(exc)[:80]})")
            return
        if text is not None:
            run.phase = "done"
            _say(app, panel, text)

    def on_done(task: asyncio.Task[Any]) -> None:
        if task.cancelled() and run.phase == "before":
            _say(app, panel, "Not changed (stopped). The page is still public.")

    tap.handlers.spawn(work(), "telegraph", on_done=on_done)
    return ""


def articles_view(app: AppContext) -> k.View:
    rows_ = tgph.pages(app)
    lines: list[k.Line] = [k.header("📖", "Articles", f"{len(rows_)} public")]
    buttons: k.Rows = []
    for i, r in enumerate(rows_[:6], 1):
        lines.append((k.Const(f"{i}. "), k.Link(k.Text(str(r["title"]), 80), str(r["url"]))))
        buttons.append([k.button(f"🗑 Unpublish {i}", "article_unpublish", {"path": str(r["path"])})])
    if not rows_:
        lines.append(k.line("No published articles."))
    return k.View(kind="fyi", blocks=[k.Block((k.Lines(tuple(lines)),))], rows=buttons)


@command("articles", help="Published Telegraph articles, with Unpublish", safety="personal")
async def articles(ctx: CommandContext, args: list[str]) -> Reply:
    return Reply("", view=articles_view(ctx.app))
