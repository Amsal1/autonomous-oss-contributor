"""Reminders (DESIGN §14.19 item 3, §14.12.7 U12; ``/remind`` and Hermes ``ossbot_reminder_create``).

A reminder stores the thread it was made in (the ``/remind`` topic, or the creating turn's ``thread_id``
for Hermes) and fires there as P1 from the gatekeeper's own scheduler (the Telegram tick; Hermes cron
stays off). During quiet hours it is held and sent loud when they end. The text is untrusted and
escaped; a Hermes reminder starts with 🤖. At most 50 are active. ``/remind`` lists them with a cancel
button each, and ``/remind <when> <text>`` makes one (``in 2h``, ``in 30m``, ``tomorrow 09:00``,
``HH:MM``, ``YYYY-MM-DD HH:MM``, local time).
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Annotated
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from ossbot.common.clock import from_iso, to_iso
from ossbot.gatekeeper import db
from ossbot.gatekeeper.telegram.actions import Tap, action
from ossbot.gatekeeper.telegram.commands.registry import CommandContext, Reply, command
from ossbot.gatekeeper.telegram.escape import esc
from ossbot.gatekeeper.telegram.runtime import ui_of
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

ACTIVE_MAX = 50
TEXT_MAX = 500
HORIZON = timedelta(days=366)
_IN = re.compile(r"^in\s+(\d{1,4})\s*(m|min|h|d)$", re.IGNORECASE)
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class RemindRefused(ValueError):
    pass


def parse_when(ctx: AppContext, words: list[str]) -> tuple[datetime, list[str]]:
    """(UTC due time, the remaining words) from the leading words; raises RemindRefused."""
    zone = ZoneInfo(ctx.config.profile.telegram.timezone)
    now = ctx.clock.now()
    local = now.astimezone(zone)
    if len(words) >= 2 and (m := _IN.match(f"{words[0]} {words[1]}")):
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {"m": timedelta(minutes=n), "min": timedelta(minutes=n), "h": timedelta(hours=n),
                 "d": timedelta(days=n)}[unit]  # fmt: skip
        return now + delta, words[2:]
    if len(words) >= 2 and words[0].lower() == "tomorrow" and (m := _HHMM.match(words[1])):
        day = local.date() + timedelta(days=1)
        return datetime.combine(day, time(int(m.group(1)), int(m.group(2))), tzinfo=zone).astimezone(now.tzinfo), words[
            2:
        ]
    if words and (m := _HHMM.match(words[0])):
        at = datetime.combine(local.date(), time(int(m.group(1)), int(m.group(2))), tzinfo=zone)
        if at <= local:
            at += timedelta(days=1)
        return at.astimezone(now.tzinfo), words[1:]
    if len(words) >= 2:
        try:
            at = datetime.strptime(f"{words[0]} {words[1]}", "%Y-%m-%d %H:%M").replace(tzinfo=zone)
        except ValueError:
            pass
        else:
            return at.astimezone(now.tzinfo), words[2:]
    raise RemindRefused("when: in 2h · in 30m · tomorrow 09:00 · 18:30 · 2026-10-20 09:00")


def create(ctx: AppContext, due_at: datetime, text: str, *, created_by: str, thread_id: int | None) -> str:
    text = " ".join(text.split())
    if not text or len(text) > TEXT_MAX:
        raise RemindRefused(f"the text is 1-{TEXT_MAX} characters")
    now = ctx.clock.now()
    if not now < due_at <= now + HORIZON:
        raise RemindRefused("the time must be in the future (within a year)")
    with db.transaction(ctx.conn):
        active = db.one(ctx.conn, "SELECT COUNT(*) AS n FROM reminders WHERE fired_at IS NULL AND cancelled_at IS NULL")
        if active is not None and int(active["n"]) >= ACTIVE_MAX:
            raise RemindRefused(f"at most {ACTIVE_MAX} active reminders")
        rid = f"r_{secrets.token_hex(6)}"
        ctx.conn.execute("INSERT INTO reminders (id, due_at, text, created_by, thread_id) VALUES (?, ?, ?, ?, ?)",
                         (rid, to_iso(due_at), text, created_by, thread_id))  # fmt: skip
        ctx.audit.append(created_by, "reminder_created", {"id": rid, "due_at": to_iso(due_at)})
    return rid


def fire_due(ctx: AppContext) -> int:
    """Send every due reminder as P1 in its thread; during quiet hours they wait (then go out loud)."""
    if ui_of(ctx).notifier.quiet_until() is not None:
        return 0
    now = to_iso(ctx.clock.now())
    rows = ctx.conn.execute("SELECT * FROM reminders WHERE fired_at IS NULL AND cancelled_at IS NULL AND due_at <= ?"
                            " ORDER BY due_at LIMIT 20", (now,)).fetchall()  # fmt: skip
    topics = ui_of(ctx).topics
    for r in rows:
        hermes = str(r["created_by"]).startswith("hermes")
        head = "🤖 ⏰ Reminder: " if hermes else "⏰ Reminder: "
        view = k.plain("fyi", (k.Const(head), k.Text(str(r["text"]), TEXT_MAX)))
        thread = r["thread_id"]
        with db.transaction(ctx.conn):
            ctx.conn.execute("UPDATE reminders SET fired_at = ? WHERE id = ? AND fired_at IS NULL", (now, r["id"]))
            dedupe = f"reminder:{r['id']}"
            if topics.mode == "flat" or thread is None:
                ui_send.deliver(ctx, view, thread="assistant" if hermes else None, priority=1, dedupe_key=dedupe)
            else:
                ui_send.deliver(ctx, view, thread_id=int(thread), priority=1, dedupe_key=dedupe)
    return len(rows)


# ------------------------------------------------------------------------------------ /remind
class CancelArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Annotated[str, Field(pattern=r"^r_[0-9a-f]{12}$")]


def list_view(ctx: AppContext) -> k.View:
    rows = ctx.conn.execute("SELECT * FROM reminders WHERE fired_at IS NULL AND cancelled_at IS NULL ORDER BY due_at"
                            " LIMIT 6").fetchall()  # fmt: skip
    lines: list[k.Line] = [k.header("⏰", "Reminders", f"{len(rows)} active")]
    buttons: k.Rows = []
    for i, r in enumerate(rows, 1):
        at = from_iso(str(r["due_at"]))
        lines.append((k.Const(f"{i}. "), k.Time(int(at.timestamp()), at.strftime("%Y-%m-%d %H:%M")), k.Const(" · "),
                      k.Text(str(r["text"]), 80)))  # fmt: skip
        buttons.append([k.button(f"🗑 Cancel {i}", "reminder_cancel", {"id": str(r["id"])}, single_use=True)])
    if not rows:
        lines.append(k.line("None. /remind in 2h check the CI · /remind tomorrow 09:00 re-check PR #5678"))
    return k.View(kind="fyi", blocks=[k.Block((k.Lines(tuple(lines)),))], rows=buttons)


@command("remind", help="List, add or cancel reminders", safety="personal")
async def remind(ctx: CommandContext, args: list[str]) -> Reply:
    app = ctx.app
    if not args:
        return Reply("", view=list_view(app))
    try:
        due_at, rest = parse_when(app, args)
        rid = create(app, due_at, " ".join(rest), created_by=ctx.actor, thread_id=ctx.thread_id)
    except RemindRefused as exc:
        return Reply(f"⏰ {esc(str(exc))}")
    when = due_at.astimezone(ZoneInfo(app.config.profile.telegram.timezone)).strftime("%a %Y-%m-%d %H:%M")
    return Reply(f"⏰ Reminder set for {esc(when)} · <code>{rid}</code>")


@action("reminder_cancel", arg=CancelArg)
async def cancel(tap: Tap) -> str:
    with db.transaction(tap.app.conn):
        cur = tap.app.conn.execute("UPDATE reminders SET cancelled_at = ? WHERE id = ? AND fired_at IS NULL AND"
                                   " cancelled_at IS NULL", (to_iso(tap.app.clock.now()), tap.arg.id))  # fmt: skip
    if cur.rowcount:
        ui_send.edit(tap.app, list_view(tap.app), message_id=tap.message_id)
    return "🗑 Cancelled." if cur.rowcount else "Already fired or cancelled."
