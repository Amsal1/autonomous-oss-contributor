"""Built-in safety commands: /status /pause /resume /kill /dryrun /help (DESIGN §14.14.1, §13).

Stop commands act immediately; ``/resume`` and ``/dryrun off`` are ``go`` and need the confirm tap.
Resume only writes ``resume-request`` (the root enforcer removes the flags, §13.2) and never
re-runs an interrupted or executing outbound action.
"""

from __future__ import annotations

from ossbot.gatekeeper.control import ControlConflict
from ossbot.gatekeeper.telegram.commands.registry import (
    COMMANDS,
    CommandContext,
    Confirm,
    Reply,
    command,
    confirm_handler,
)
from ossbot.gatekeeper.telegram.escape import b, esc
from ossbot.gatekeeper.telegram.runtime import ui_of

REASON_MAX = 300


def _reason(args: list[str], default: str) -> str:
    return (" ".join(args).strip() or default)[:REASON_MAX]


@command("status", help="The dashboard card (state, dry-run, work, approvals, config and audit heads)",
         safety="read")  # fmt: skip
async def status(ctx: CommandContext, args: list[str]) -> Reply:
    """The same card as the pinned dashboard (one builder), sent into the topic it was asked from; its
    buttons are its own tokens, so taps act on this copy (DESIGN §14.14.1)."""
    return Reply("", view=ui_of(ctx.app).dashboard.view(None, kind="status"))


@command("pause", help="Pause the pipeline (immediate)", safety="stop")
async def pause(ctx: CommandContext, args: list[str]) -> Reply:
    view = ctx.app.control.pause(_reason(args, "telegram /pause"), ctx.actor)
    return Reply(f"⏸ Paused. State: {b(view.state)}")


@command("kill", help="Soft kill: stop all work and outbound actions (immediate)", safety="stop")
async def kill(ctx: CommandContext, args: list[str]) -> Reply:
    view = ctx.app.control.kill(_reason(args, "telegram /kill"), ctx.actor, "soft")
    return Reply(f"🛑 Killed ({esc(view.kill_level or 'soft')}). Use /resume to continue.")


@command("resume", help="Resume after pause or kill (asks for confirmation)", safety="go")
async def resume(ctx: CommandContext, args: list[str]) -> Reply:
    view = ctx.app.control.view()
    if view.state == "running":
        return Reply("Already running.")
    return Reply(
        f"Resume from {b(view.state)}? Interrupted submissions stay failed; nothing is re-sent.",
        Confirm("resume_confirm", "▶️ Confirm resume"),
    )


@confirm_handler("resume_confirm")
async def resume_confirmed(ctx: CommandContext, arg: str | None) -> Reply:
    try:
        ctx.app.control.request_resume(ctx.actor, "telegram /resume")
    except ControlConflict:
        return Reply("Already running.")
    return Reply("▶️ Resume requested; the root enforcer lifts the flags and restarts the services.")


@command("dryrun", help="/dryrun on (immediate) or /dryrun off (asks for confirmation)", safety="go")
async def dryrun(ctx: CommandContext, args: list[str]) -> Reply:
    mode = args[0].lower() if args else ""
    current = ctx.app.control.view().dry_run
    if mode == "on":
        invalidated = ctx.app.control.set_dry_run(True, ctx.actor)
        ctx.app.reissue_cards(invalidated, ctx.actor)
        return Reply(f"Dry-run is {b('ON')}. {len(invalidated)} open approval(s) re-issued as DRY RUN.")
    if mode == "off":
        if not current:
            return Reply("Dry-run is already OFF (LIVE).")
        return Reply(
            f"Turn dry-run {b('OFF')}? Approved changes will then be published for real.",
            Confirm("dryrun_off_confirm", "⚠️ Confirm LIVE mode", style="danger"),
        )
    return Reply(f"Dry-run is {b('ON' if current else 'OFF')}. Usage: /dryrun on | /dryrun off")


@confirm_handler("dryrun_off_confirm")
async def dryrun_off_confirmed(ctx: CommandContext, arg: str | None) -> Reply:
    invalidated = ctx.app.control.set_dry_run(False, ctx.actor)
    ctx.app.reissue_cards(invalidated, ctx.actor)
    return Reply(f"Dry-run is {b('OFF')} (LIVE). {len(invalidated)} open approval(s) re-issued as LIVE.")


@command("start", help="What ossbot is and where things are", safety="read")
async def start(ctx: CommandContext, args: list[str]) -> Reply:
    mode = ui_of(ctx.app).topics.mode
    where = ("🚦 Control (dashboard, commands) · ✅ Reviews (every card) · 📈 OSS feed · 💬 Assistant"
             if mode == "topics" else
             "one chat; each message starts with its section icon (🚦 ✅ 📈 💬). Turn on Threaded mode in"
             " @BotFather (Bot Settings → Threads Settings) for separate threads.")  # fmt: skip
    return Reply(
        f"{b('ossbot')}: open-source contributions you approve, plus your assistant.\n"
        f"Where things are: {esc(where)}\n"
        "Nothing is published without your two taps on a card. /status · /queue · /help"
    )


@command("dashboard", help="Re-send and pin the live dashboard", safety="read")
async def dashboard(ctx: CommandContext, args: list[str]) -> Reply:
    ui_of(ctx.app).dashboard.send_new()
    return Reply("📌 Dashboard re-sent into 🚦 Control and pinned there.")


@command("help", help="List commands", safety="read")
async def help_(ctx: CommandContext, args: list[str]) -> Reply:
    lines = [f"/{spec.name} — {esc(spec.help)}" for spec in sorted(COMMANDS.values(), key=lambda s: s.name)]
    return Reply("\n".join(lines))
