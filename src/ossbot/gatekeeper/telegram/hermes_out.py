"""Where unsolicited Hermes messages go (DESIGN §14.12.7 U12, AC 35, BA-22).

Feature cards, ``POST /v1/notify`` from Hermes and reminders made by ``ossbot_reminder_create`` attach to
the turn that the plugin's ``X-Hermes-Session-Key`` names, and only when the gatekeeper's own
``chat_turns`` has a ``streaming`` row of that session started in the last 30 min or a row finished in
the last 10 min (``TurnStore.active``). Anything else is 409 ``no_active_turn`` and nothing is sent.

The resolved thread must be 💬 Assistant or a user chat topic; a Reviews session's messages go to 💬
Assistant with ``🤖 from your Reviews chat`` (Control and OSS feed likewise): no unsolicited Hermes
message ever lands in ✅ Reviews, 🚦 Control or 📈 OSS feed. Flat mode renders it under the gatekeeper
header ``🤖 Assistant · <feature or "notice">`` with the whole body in one quote (U6), buttons kept.
Every one carries 🤖 in its header line. Hermes gets at most 5 P1 messages per local day; later P1
requests go out as P2 and are counted for the digest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ossbot.common.clock import to_iso
from ossbot.gatekeeper import db
from ossbot.gatekeeper.telegram.runtime import ui_of
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

log = logging.getLogger(__name__)
HERMES_P1_PER_DAY = 5
FROM_REVIEWS = "🤖 from your Reviews chat"
SYSTEM_TOPICS_FORBIDDEN = frozenset({"reviews", "control", "feed"})


@dataclass(frozen=True)
class Target:
    turn_id: int
    thread_id: int | None  # a user chat topic, or None (assistant / flat)
    assistant: bool  # send to the 💬 Assistant system topic
    from_system: bool  # the session's own thread was Reviews/Control/Feed (gets the 🤖 from … line)


def resolve(ctx: AppContext, session_key: str | None) -> Target | None:
    if ctx.chat is None or not session_key or len(session_key) > 256:
        return None
    row = ctx.chat.service.turns.active(session_key)
    if row is None:
        log.info("unsolicited Hermes message without an active turn", extra={"event": "no_active_turn"})
        return None
    topics = ui_of(ctx).topics
    thread = row["thread_id"]
    if topics.mode == "flat" or thread is None:
        return Target(int(row["turn_id"]), None, True, False)
    name = topics.name_of(int(thread))
    if name is None:  # a user chat topic
        return Target(int(row["turn_id"]), int(thread), False, False)
    return Target(int(row["turn_id"]), None, True, name in SYSTEM_TOPICS_FORBIDDEN)


def _p1_today(ctx: AppContext) -> int:
    zone = ZoneInfo(ctx.config.profile.telegram.timezone)
    local = ctx.clock.now().astimezone(zone)
    midnight = datetime.combine(local.date(), time(0, 0), tzinfo=zone)
    return ctx.audit.count_since("hermes_p1", to_iso(midnight.astimezone(ctx.clock.now().tzinfo)))


def hermes_priority(ctx: AppContext, requested: str) -> int:
    """P1 or P2; the sixth Hermes P1 of a local day goes out as P2 and is counted (digest)."""
    if requested != "P1":
        return 2
    with db.transaction(ctx.conn):
        if _p1_today(ctx) >= HERMES_P1_PER_DAY:
            ctx.audit.append("hermes", "hermes_p1_demoted", {})
            return 2
        ctx.audit.append("hermes", "hermes_p1", {})
    return 1


def flat_wrap(view: k.View, label: str) -> k.View:
    """Flat mode (U6): the gatekeeper header, then the whole body in one quote; buttons stay."""
    lines: list[k.Line] = []
    for block in view.blocks:
        for el in block.elements:
            if isinstance(el, k.Lines | k.Quote):
                lines += list(el.lines)
            elif isinstance(el, k.FooterEl):
                lines.append(el.line)
            elif isinstance(el, k.PreBlock | k.Markdown):
                lines += [(k.Text(ln),) for ln in el.text.splitlines()]
            elif isinstance(el, k.DetailsEl):
                lines += [el.summary, *el.lines]
    head = k.Block((k.Lines(((k.Const("🤖 "), k.Bold(k.Const("Assistant")), k.Const(f" · {label}")),)),), name="head")
    body = k.Block((k.Quote(tuple(lines) or ((k.Const(" "),),), expandable=True),), name="body")
    return replace(view, blocks=[head, body])


def deliver(ctx: AppContext, target: Target, view: k.View, *, label: str, priority: int) -> int | None:
    """Send an unsolicited Hermes view to its resolved thread (never Reviews, Control or OSS feed)."""
    flat = ui_of(ctx).topics.mode == "flat"
    if flat:
        view = flat_wrap(view, label)
    if target.from_system and not flat:
        view = replace(view, blocks=[k.Block((k.Lines(((k.Const(FROM_REVIEWS),),)),), name="from"), *view.blocks])
    holdable = priority >= 2 and not view.rows  # a P2 notice may wait for quiet hours; buttons never do
    if target.assistant:
        return ui_send.deliver(ctx, view, thread="assistant", priority=priority, holdable=holdable)
    return ui_send.deliver(ctx, view, thread_id=target.thread_id, priority=priority, holdable=holdable)


def notice_view(text: str) -> k.View:
    """A Hermes notification: ``🤖`` leads its header line (it can't look like a gatekeeper fact)."""
    return k.View(kind="fyi", blocks=[k.Block((k.Lines(((k.Const("🤖 "), k.Text(text, 3500)),)),))])
