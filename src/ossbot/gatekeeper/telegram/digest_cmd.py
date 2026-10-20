"""``/digest`` (DESIGN §14.13.4): ``[📨 Send now] [Daily ✅] [Weekly ✅] [⏰ Times…]``, personal class.

The toggles are the ``/notify`` categories ``digest_daily`` and ``digest_weekly`` (off = not generated).
``Send now`` builds today's digest in a spawned task (it reads GitHub for the followers and stars).
The times live in ``telegram.yaml`` (a confirmed ``/reload`` changes them).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import digest
from ossbot.gatekeeper.telegram.actions import Tap, action
from ossbot.gatekeeper.telegram.commands.registry import CommandContext, Reply, command
from ossbot.gatekeeper.telegram.runtime import ui_of
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send


class KindArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["daily", "weekly"]


def digest_view(app: AppContext) -> k.View:
    toggles = ui_of(app).notifier.toggles()
    cfg = app.config.telegram.digest
    mark = {True: "✅", False: "⬜"}
    lines = (k.header("📈", "Digest"), k.line(f"Daily at {cfg.daily} · weekly {cfg.weekly} (local time)"))
    rows: k.Rows = [
        [k.button("📨 Send now", "digest_send_now", {"kind": "daily"})],
        [k.button(f"Daily {mark[toggles.get('digest_daily', True)]}", "digest_toggle", {"kind": "daily"}),
         k.button(f"Weekly {mark[toggles.get('digest_weekly', True)]}", "digest_toggle", {"kind": "weekly"})],
        [k.button("⏰ Times…", "digest_times")],
    ]  # fmt: skip
    return k.View(kind="picker", blocks=[k.Block((k.Lines(lines),))], rows=rows)


@command("digest", help="Daily and weekly digest: send now, on/off, times", safety="personal")
async def digest_cmd(ctx: CommandContext, args: list[str]) -> Reply:
    return Reply("", view=digest_view(ctx.app))


@action("digest_toggle", arg=KindArg)
async def toggle(tap: Tap) -> str:
    notifier = ui_of(tap.app).notifier
    key = f"digest_{tap.arg.kind}"
    on = not notifier.toggles().get(key, True)
    notifier.set_toggle(key, on)
    ui_send.edit(tap.app, digest_view(tap.app), message_id=tap.message_id)
    return f"{tap.arg.kind.capitalize()} digest {'on' if on else 'off'}."


@action("digest_send_now", arg=KindArg, effect="io")
async def send_now(tap: Tap) -> str:
    period = digest.now_period(tap.app, tap.arg.kind)
    sent = await digest.run(tap.app, period, getattr(tap.handlers, "reader", None))
    return "📨 Digest sent to 📈 OSS feed." if sent is not None else "📨 Today's digest was already sent."


@action("digest_times")
async def times(tap: Tap) -> str:
    return "Set telegram.digest.daily / weekly in telegram.yaml, then /reload."
