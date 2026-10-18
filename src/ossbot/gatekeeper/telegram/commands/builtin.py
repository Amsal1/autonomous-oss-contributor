"""Built-in safety commands: /status /pause /resume /kill /dryrun /help (DESIGN §14.5, §13).

Stop commands act immediately; ``/resume`` and ``/dryrun off`` are ``go`` and need the confirm tap.
Resume only writes ``resume-request`` (the root enforcer removes the flags, §13.2) and never
re-runs an interrupted or executing outbound action.
"""

from __future__ import annotations

from ossbot.gatekeeper import ratelimit
from ossbot.gatekeeper.control import ControlConflict
from ossbot.gatekeeper.telegram.commands.registry import (
    COMMANDS,
    CommandContext,
    Confirm,
    Reply,
    command,
    confirm_handler,
)
from ossbot.gatekeeper.telegram.escape import b, code, esc

REASON_MAX = 300


def _reason(args: list[str], default: str) -> str:
    return (" ".join(args).strip() or default)[:REASON_MAX]


@command("status", help="Control state, dry-run, config and audit heads, queue counts", safety="read")
async def status(ctx: CommandContext, args: list[str]) -> Reply:
    app = ctx.app
    view = app.control.view()
    counts = app.conn.execute("SELECT state, COUNT(*) AS n FROM contributions GROUP BY state ORDER BY state").fetchall()
    limits = app.config.policy.limits
    used = {kind: ratelimit.used_in_window(app.conn, app.clock, kind) for kind in ("open_pr", "comment", "issue_claim")}
    lines = [
        f"{b('ossbot')} · state {b(view.state.upper())}"
        + (f" ({esc(view.kill_level)})" if view.kill_level else "")
        + f" · {'<b>DRY RUN</b>' if view.dry_run else '<b>LIVE</b>'}",
        f"Reason: {esc(view.reason or '-')} · since {esc(view.since)}",
        f"Config {code(view.config_hash[:12])} · audit head {code(view.audit_head[:12])}",
        f"Today: PRs {used['open_pr']}/{limits.prs_per_24h} · comments {used['comment']}/{limits.comments_per_24h}"
        f" · claims {used['issue_claim']}/{limits.issue_claims_per_24h}",
    ]
    if counts:
        lines.append("Contributions: " + ", ".join(f"{esc(r['state'])} {r['n']}" for r in counts))
    poll = app.poll_stats.as_dict()
    lines.append(
        f"Lifecycle {b(view.lifecycle)} · Telegram last poll {esc(poll['last_ok_at'] or '-')}"
        f" · reconnects {poll['reconnects']}"
    )
    return Reply("\n".join(lines))


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
            Confirm("dryrun_off_confirm", "⚠️ Confirm LIVE mode"),
        )
    return Reply(f"Dry-run is {b('ON' if current else 'OFF')}. Usage: /dryrun on | /dryrun off")


@confirm_handler("dryrun_off_confirm")
async def dryrun_off_confirmed(ctx: CommandContext, arg: str | None) -> Reply:
    invalidated = ctx.app.control.set_dry_run(False, ctx.actor)
    ctx.app.reissue_cards(invalidated, ctx.actor)
    return Reply(f"Dry-run is {b('OFF')} (LIVE). {len(invalidated)} open approval(s) re-issued as LIVE.")


@command("help", help="List commands", safety="read")
async def help_(ctx: CommandContext, args: list[str]) -> Reply:
    lines = [f"/{spec.name} — {esc(spec.help)}" for spec in sorted(COMMANDS.values(), key=lambda s: s.name)]
    return Reply("\n".join(lines))
