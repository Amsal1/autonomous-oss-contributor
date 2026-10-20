"""Personal and read commands (DESIGN §14.14.1, §14.13.2; PLAN item 47): ``/settings``, ``/quiet``,
``/notify``, ``/new`` (``/newchat``), ``/limits`` (``/budget``), ``/logs`` and ``/skills``.

``personal`` changes are presentation-only (``ux_prefs``): immediate, no OSS, GitHub or control effect.
P0 alerts ring whatever ``/quiet`` or ``/notify`` say (U7), and a toggle mutes or batches but never
drops a card (§14.13.2).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from ossbot.common.clock import to_iso
from ossbot.common.ids import ID_RE
from ossbot.gatekeeper import db, ratelimit
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.actions import Tap, action
from ossbot.gatekeeper.telegram.commands.common import picker_rows
from ossbot.gatekeeper.telegram.commands.registry import CommandContext, Reply, command
from ossbot.gatekeeper.telegram.escape import b, esc
from ossbot.gatekeeper.telegram.notify import CATEGORIES
from ossbot.gatekeeper.telegram.runtime import ui_of
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import redact as ui_redact
from ossbot.gatekeeper.telegram.ui import send as ui_send

QuietChoice = Literal["1h", "3h", "0800", "tomorrow", "off"]
QUIET_LABELS: dict[str, str] = {"1h": "1 h", "3h": "3 h", "0800": "Until 08:00", "tomorrow": "Until tomorrow",
                                "off": "Off"}  # fmt: skip
TAIL_LINES = 30
TAIL_READ = 64 * 1024
LOG_DOC_MAX = 1024 * 1024
RICH_NEXT = {"auto": "on", "on": "off", "off": "auto"}
MIRROR_INFO = "The review mirror is enabled in config only (telegram.yaml), then /reload (U9)."


# ------------------------------------------------------------------------------------ /quiet
def quiet_until(app: AppContext, choice: str) -> datetime | None:
    now = app.clock.now()
    zone = ZoneInfo(app.config.profile.telegram.timezone)
    local = now.astimezone(zone)
    if choice == "1h":
        return now + timedelta(hours=1)
    if choice == "3h":
        return now + timedelta(hours=3)
    if choice in {"0800", "tomorrow"}:
        day = local.date() + timedelta(days=1)
        if choice == "0800" and local.time() < time(8, 0):
            day = local.date()
        return datetime.combine(day, time(8, 0), tzinfo=zone).astimezone(now.tzinfo)
    return None


def quiet_line(app: AppContext) -> k.Line:
    until = ui_of(app).notifier.quiet_until()
    if until is None:
        return k.line("🔔 Not quiet: P1 rings, P2 arrives silently. P0 alerts always ring.")
    return (k.Const("🌙 Quiet until "), k.Time(int(until.timestamp()), until.strftime("%H:%M")),
            k.Const(" (P0 alerts still ring)"))  # fmt: skip


def quiet_view(app: AppContext) -> k.View:
    opts = [k.button(label, "quiet_pick", {"choice": c}) for c, label in QUIET_LABELS.items()]
    return k.plain("picker", quiet_line(app), rows=picker_rows(opts))


class QuietPick(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choice: QuietChoice


@command("quiet", help="Quiet mode: P1 silent, P2 held (P0 still rings)", safety="personal")
async def quiet(ctx: CommandContext, args: list[str]) -> Reply:
    return Reply("", view=quiet_view(ctx.app))


@action("quiet_pick", arg=QuietPick)
async def quiet_pick(tap: Tap) -> str:
    until = quiet_until(tap.app, tap.arg.choice)
    prefs = ui_of(tap.app).prefs
    prefs.set("quiet_until", to_iso(until) if until is not None else None)
    with db.transaction(tap.app.conn):
        tap.app.audit.append(tap.actor, "quiet_set", {"until": to_iso(until) if until else None})
    ui_send.edit(tap.app, quiet_view(tap.app), message_id=tap.message_id)
    return "🌙 Quiet on." if until is not None else "🔔 Quiet off."


# ----------------------------------------------------------------------------------- /notify
class NotifyToggle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str = Field(min_length=1, max_length=20)


def notify_view(app: AppContext) -> k.View:
    toggles = ui_of(app).notifier.toggles()
    btns = [k.button(f"{'✅' if toggles[c] else '⬜'} {label}", "notify_toggle", {"category": c})
            for c, label in CATEGORIES.items()]  # fmt: skip
    return k.plain("picker", (k.Const("🔕 "), k.Bold(k.Const("Notifications"))),
                   k.line("Off = silent (P2 batched); cards are always delivered. P0 alerts have no toggle."),
                   rows=picker_rows(btns, 2))  # fmt: skip


@command("notify", help="Notification toggles per category (mute or batch, never drop)", safety="personal")
async def notify(ctx: CommandContext, args: list[str]) -> Reply:
    return Reply("", view=notify_view(ctx.app))


@action("notify_toggle", arg=NotifyToggle)
async def notify_toggle(tap: Tap) -> str:
    notifier = ui_of(tap.app).notifier
    if tap.arg.category not in CATEGORIES:
        return "This button is no longer valid."
    on = not notifier.toggles()[tap.arg.category]
    notifier.set_toggle(tap.arg.category, on)
    ui_send.edit(tap.app, notify_view(tap.app), message_id=tap.message_id)
    return f"{CATEGORIES[tap.arg.category]}: {'on' if on else 'silent'}."


# --------------------------------------------------------------------------------- /settings
class SettingsPick(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: Literal["rich", "notify", "quiet", "digest", "mirror", "main"]


def settings_view(app: AppContext) -> k.View:
    rich = str(ui_of(app).prefs.get("rich_views", "auto"))
    rows = [[k.button(f"🖼 Rich views: {rich}", "settings_pick", {"item": "rich"}),
             k.button("🔕 Notifications…", "settings_pick", {"item": "notify"})],
            [k.button("🌙 Quiet hours…", "settings_pick", {"item": "quiet"}),
             k.button("📰 Digest…", "settings_pick", {"item": "digest"})],
            [k.button("🐙 Mirror: off ⓘ", "settings_pick", {"item": "mirror"})]]  # fmt: skip
    return k.plain("picker", (k.Const("⚙️ "), k.Bold(k.Const("Settings"))), quiet_line(app), rows=rows)


@command("settings", help="Rich views, notifications, quiet hours, digest", safety="personal")
async def settings(ctx: CommandContext, args: list[str]) -> Reply:
    return Reply("", view=settings_view(ctx.app))


@action("settings_pick", arg=SettingsPick)
async def settings_pick(tap: Tap) -> str:
    item = tap.arg.item
    if item == "mirror":
        return MIRROR_INFO
    if item == "digest":
        return "Digest times and toggles: /digest."
    if item == "rich":
        prefs = ui_of(tap.app).prefs
        prefs.set("rich_views", RICH_NEXT.get(str(prefs.get("rich_views", "auto")), "auto"))
    view = notify_view(tap.app) if item == "notify" else quiet_view(tap.app) if item == "quiet" else settings_view(
        tap.app)  # fmt: skip
    ui_send.edit(tap.app, view, message_id=tap.message_id)
    return ""


# -------------------------------------------------------------------------------------- /new
async def _new(ctx: CommandContext) -> Reply:
    if ctx.app.chat is None:
        return Reply("🤖 The assistant is not configured.")
    ctx.app.chat.service.turns.new_session(ctx.tg_user_id)
    return Reply("💬 New Hermes conversation: your next message starts fresh.")


@command("new", help="Start a new Hermes conversation", safety="personal")
async def new(ctx: CommandContext, args: list[str]) -> Reply:
    return await _new(ctx)


@command("newchat", help="Alias of /new", safety="personal")
async def newchat(ctx: CommandContext, args: list[str]) -> Reply:
    return await _new(ctx)


# ----------------------------------------------------------------------------------- /limits
async def _limits(ctx: CommandContext) -> Reply:
    app = ctx.app
    lim = app.config.policy.limits
    rows: list[tuple[str, Literal["open_pr", "comment", "issue_claim"], int]] = [
        ("PRs", "open_pr", lim.prs_per_24h), ("Comments", "comment", lim.comments_per_24h),
        ("Issue claims", "issue_claim", lim.issue_claims_per_24h)]  # fmt: skip
    lines = [f"📊 {b('Usage vs limits')} (rolling 24 h)"]
    lines += [f"{name}: {ratelimit.used_in_window(app.conn, app.clock, kind)}/{cap}" for name, kind, cap in rows]
    lines.append(f"Approvals expire after {lim.approval_ttl_hours} h · confirms after {lim.confirm_ttl_seconds} s")
    return Reply("\n".join(lines))


@command("limits", help="Usage vs limits", safety="read")
async def limits(ctx: CommandContext, args: list[str]) -> Reply:
    return await _limits(ctx)


@command("budget", help="Alias of /limits", safety="read")
async def budget(ctx: CommandContext, args: list[str]) -> Reply:
    return await _limits(ctx)


# ------------------------------------------------------------------------------------- /logs
class LogsArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cid: str | None = Field(default=None, max_length=20)
    run: str | None = Field(default=None, max_length=40)


def logs_contributions(app: AppContext) -> k.View:
    rows_ = app.conn.execute("SELECT id, state FROM contributions ORDER BY updated_at DESC LIMIT 8").fetchall()
    btns = [k.button(f"{r['id']} · {str(r['state'])[:9]}", "logs_pick", {"cid": r["id"]}) for r in rows_]
    text = "📜 Logs: which contribution?" if btns else "📜 No contributions yet."
    return k.plain("picker", k.line(text), rows=picker_rows(btns, 2))


def logs_stages(app: AppContext, cid: str) -> k.View:
    runs = app.conn.execute("SELECT id, stage, attempt, status FROM stage_runs WHERE cid = ? ORDER BY"
                            " COALESCE(started_at, '') DESC LIMIT 8", (cid,)).fetchall()  # fmt: skip
    btns = [k.button(f"{r['stage']} #{r['attempt']} {r['status'][:6]}", "logs_pick", {"cid": cid, "run": r["id"]})
            for r in runs]  # fmt: skip
    rows = picker_rows(btns, 2)
    rows.append([k.button("↩️ Back", "logs_pick", {})])
    return k.plain("picker", (k.Const("📜 "), k.Text(cid), k.Const(": which stage?" if btns else ": no stage runs.")),
                   rows=rows)  # fmt: skip


def _read_log(app: AppContext, log_path: str | None) -> bytes | None:
    """A stage log under the ossbot logs dir only (the path is runner-reported data, never trusted)."""
    if not log_path:
        return None
    try:
        root = app.paths.logs_dir.resolve()
        path = Path(log_path).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return None
        with path.open("rb") as fh:
            fh.seek(max(0, path.stat().st_size - LOG_DOC_MAX))
            return fh.read(LOG_DOC_MAX)
    except OSError:
        return None


def log_reply(app: AppContext, run_id: str) -> Reply:
    run = db.one(app.conn, "SELECT * FROM stage_runs WHERE id = ?", (run_id,))
    if run is None:
        return Reply("Unknown stage run.")
    head = (f"📜 {b(str(run['stage']))} #{run['attempt']} of {esc(str(run['cid']))} · {esc(str(run['status']))}"
            + (f" · {esc(str(run['error_class']))}" if run["error_class"] else ""))  # fmt: skip
    data = _read_log(app, run["log_path"])
    if data is None:
        detail = ui_redact.redact(str(run["error_message"] or "no log file"), surface="logs")[0]
        return Reply(f"{head}\n<pre>{esc(detail[:1500])}</pre>")
    text = ui_redact.redact(data.decode("utf-8", "replace"), surface="logs")[0]
    tail = "\n".join(text[-TAIL_READ:].splitlines()[-TAIL_LINES:])
    return Reply(f"{head}\n<pre>{esc(tail[-3000:])}</pre>",
                 document=(f"{run['cid']}-{run['stage']}-{run['attempt']}.txt", text.encode("utf-8")))  # fmt: skip


@command("logs", help="Stage logs: /logs [cid] [stage]", safety="read")
async def logs(ctx: CommandContext, args: list[str]) -> Reply:
    app = ctx.app
    if not args:
        return Reply("", view=logs_contributions(app))
    cid = args[0]
    if not ID_RE.match(cid) or not cid.startswith("c_"):
        return Reply("Usage: /logs [c_0123456789] [stage]")
    if len(args) > 1:
        run = db.one(app.conn, "SELECT id FROM stage_runs WHERE cid = ? AND stage = ? ORDER BY"
                     " COALESCE(started_at, '') DESC LIMIT 1", (cid, args[1]))  # fmt: skip
        return log_reply(app, str(run["id"])) if run is not None else Reply("No such stage run.")
    return Reply("", view=logs_stages(app, cid))


@action("logs_pick", arg=LogsArg)
async def logs_pick(tap: Tap) -> str:
    a: LogsArg = tap.arg
    if a.run is not None:
        tap.handlers._send_reply(log_reply(tap.app, a.run), thread_id=tap.thread_id)
        return ""
    view = logs_stages(tap.app, a.cid) if a.cid else logs_contributions(tap.app)
    if tap.app.control.view().state == "killed" and a.cid is None:
        tap.handlers._send_reply(Reply("", view=view), thread_id=tap.thread_id)  # keep the killed message
        return ""
    ui_send.edit(tap.app, view, message_id=tap.message_id)
    return ""


# ----------------------------------------------------------------------------------- /skills
@command("skills", help="Staged and active Hermes skills", safety="read")
async def skills(ctx: CommandContext, args: list[str]) -> Reply:
    app = ctx.app
    rows_ = app.conn.execute("SELECT skill, version, status, action FROM skill_reviews WHERE status IN ('staged',"
                             "'activating','activated') ORDER BY skill, version DESC").fetchall()  # fmt: skip
    seen: set[str] = set()
    lines: list[k.Line] = [(k.Const("🧠 "), k.Bold(k.Const("Skills")))]
    btns: list[k.ButtonSpec] = []
    for r in rows_:
        if r["skill"] in seen:
            continue
        seen.add(str(r["skill"]))
        lines.append((k.Text(str(r["skill"]), 64), k.Const(f" v{r['version']} · {r['status']}")))
        if r["status"] == "staged" and len(btns) < 8:
            btns.append(k.button(f"Open {str(r['skill'])[:17]}", "queue_open", {"family": "s", "subject": r["skill"]}))
    if not seen:
        lines.append(k.line("No skills yet. Hermes stages new ones for your approval (tier self_improve)."))
    return Reply("", view=k.View(kind="picker", blocks=[k.Block((k.Lines(tuple(lines)),))],
                                 rows=picker_rows(btns, 2)))  # fmt: skip
