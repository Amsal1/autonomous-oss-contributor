"""Lifecycle commands (DESIGN §14.14.1, §14.16, CR-001 5, PLAN items 42 and 47): ``/drain`` (stop,
immediate), ``/undrain`` (go, confirm), ``/down`` (stop + confirm: timeout picker, then confirm) and ``/up``
(go, confirm; refused while killed). ``/down`` and ``/up`` only hand a workers-scope request to the root
helper (``ControlService.request_lifecycle``); the gatekeeper stays up, so ``/up`` keeps working.

Also the killed-mode reply of §14.16 (every non-allowed command while killed).
"""

from __future__ import annotations

import contextlib

from pydantic import BaseModel, ConfigDict, Field

from ossbot.common.clock import from_iso
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.control import ControlConflict
from ossbot.gatekeeper.telegram.actions import Tap, action
from ossbot.gatekeeper.telegram.commands.common import confirm_button, confirm_panel, picker_rows
from ossbot.gatekeeper.telegram.commands.registry import CommandContext, Confirm, Reply, command, confirm_handler
from ossbot.gatekeeper.telegram.escape import b, esc
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send

DOWN_TIMEOUTS = (60, 90, 120)
KILLED_UP = "Killed: /up is refused. /resume first (kill overrides lifecycle)."


def _state(app: AppContext) -> str | None:
    info = app.control.lifecycle()
    return None if info is None else info.state


def _killed(app: AppContext) -> bool:
    return app.control.view().state == "killed"


# ------------------------------------------------------------------------------------ drain
@command("drain", help="Drain: no new work, running work may finish (immediate)", safety="stop")
async def drain(ctx: CommandContext, args: list[str]) -> Reply:
    timeout = ctx.app.config.policy.lifecycle.drain_timeout_s
    ctx.app.control.drain(ctx.actor, "telegram /drain", timeout, "workers")
    return Reply(f"⏬ {b('Draining')}: no new work; running work gets up to {timeout} s. Approvals stay valid."
                 " /undrain to schedule again.")  # fmt: skip


@command("undrain", help="Stop draining and schedule work again (asks for confirmation)", safety="go")
async def undrain(ctx: CommandContext, args: list[str]) -> Reply:
    if _state(ctx.app) != "draining":
        return Reply("Not draining.")
    return Reply("Undrain: scheduling restarts; approvals stay valid.", Confirm("undrain_confirm", "✅ Undrain"))


@confirm_handler("undrain_confirm")
async def undrain_confirmed(ctx: CommandContext, arg: str | None) -> Reply:
    if _state(ctx.app) != "draining":
        return Reply("Not draining; nothing changed.")
    try:
        ctx.app.control.up(ctx.actor)
    except ControlConflict:
        return Reply(esc(KILLED_UP))
    return Reply(f"▶️ {b('Undrained')}: scheduling restarts.")


# ------------------------------------------------------------------------------------- down
class DownArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeout_s: int = Field(ge=30, le=600)


def down_sentence(timeout_s: int) -> str:
    return (f"Down (workers): running stages get {timeout_s} s, then re-queue. The gatekeeper stays up so /up"
            " works.")  # fmt: skip


def down_confirm_view(timeout_s: int) -> k.View:
    return confirm_panel([k.line(down_sentence(timeout_s))],
                         confirm_button("✅ Down (workers)", "down_confirm", str(timeout_s)))  # fmt: skip


def down_picker() -> k.View:
    opts = [k.button(f"{t} s", "down_pick", {"timeout_s": t}, single_use=True) for t in DOWN_TIMEOUTS]
    return k.plain("picker", k.line("⬇️ Down (workers): how long may running stages finish?"),
                   rows=picker_rows(opts))  # fmt: skip


@command("down", help="Down (workers): stop the workers, keep the gatekeeper up", safety="stop")
async def down(ctx: CommandContext, args: list[str]) -> Reply:
    """Disruptive, so one confirm even though it is a stop (a stale /down produces only its picker)."""
    if _state(ctx.app) == "down":
        return Reply("Already 💤 DOWN (workers). /up to start them.")
    if args and args[0].rstrip("s").isdigit() and int(args[0].rstrip("s")) in DOWN_TIMEOUTS:
        return Reply("", view=down_confirm_view(int(args[0].rstrip("s"))))
    return Reply("", view=down_picker())


@action("down_pick", arg=DownArg)
async def down_pick(tap: Tap) -> str:
    ui_send.edit(tap.app, down_confirm_view(tap.arg.timeout_s), message_id=tap.message_id)
    return ""


@confirm_handler("down_confirm", effect="io")
async def down_confirmed(ctx: CommandContext, arg: str | None) -> Reply:
    timeout = int(arg) if arg and arg.isdigit() and int(arg) in DOWN_TIMEOUTS else DOWN_TIMEOUTS[1]
    try:
        ctx.app.control.request_lifecycle("down", ctx.actor, timeout, "workers")
    except ControlConflict as exc:
        return Reply(f"Refused: {esc(str(exc)[:200])}")
    return Reply(f"⬇️ {b('Down (workers) requested')}: running stages get {timeout} s, then re-queue."
                 " The bot stays 🟢 Online; /up starts them again.")  # fmt: skip


# --------------------------------------------------------------------------------------- up
@command("up", help="Start the workers again (asks for confirmation)", safety="go")
async def up(ctx: CommandContext, args: list[str]) -> Reply:
    if _killed(ctx.app):
        return Reply(esc(KILLED_UP))
    if _state(ctx.app) is None:
        return Reply("Already up.")
    return Reply("Up: the workers start and interrupted stages re-queue; approvals stay valid.",
                 Confirm("up_confirm", "✅ Up"))  # fmt: skip


@confirm_handler("up_confirm", effect="io")
async def up_confirmed(ctx: CommandContext, arg: str | None) -> Reply:
    try:
        ctx.app.control.request_lifecycle("up", ctx.actor, 0, "workers")
    except ControlConflict:
        return Reply(esc(KILLED_UP))
    return Reply(f"⬆️ {b('Up requested')}: the workers start; you get a “Back up” note when they run.")


# ----------------------------------------------------------------------------- killed (§14.16)
def killed_view(app: AppContext) -> k.View:
    """``🛑 Killed (soft) since <tg-time> · reason <code>…</code>. Only /status, /resume, /logs, /help and
    /start work.`` with ``[▶️ Resume…] [📜 Logs]``."""
    view = app.control.view()
    level = view.kill_level or "soft"
    head: list[k.Span] = [k.Const("🛑 "), k.Bold(k.Const(f"Killed ({level})"))]
    with contextlib.suppress(TypeError, ValueError):  # no or unreadable "since": the line goes without it
        head += [k.Const(" since "), k.Time(int(from_iso(view.since).timestamp()), view.since[11:16])]
    head += [k.Const(" · reason "), k.Code(k.Text(view.reason or "-", 80))]
    rows = [[k.button("▶️ Resume…", "dash_resume"), k.button("📜 Logs", "logs_pick", {})]]
    return k.plain("fyi", tuple(head), k.line("Only /status, /resume, /logs, /help and /start work."), rows=rows)
