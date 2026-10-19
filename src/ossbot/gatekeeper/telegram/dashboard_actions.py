"""Dashboard buttons (DESIGN §14.10.3, D14, AC 15): Refresh, Approvals, Pause / Resume…, Quiet 1h,
Down… and the two-tap Kill. Pause and Kill are stop actions (immediate; Kill needs its second tap
within 10 s); Resume and Down only open their confirm or picker, exactly like the typed commands."""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field

from ossbot.common.clock import to_iso
from ossbot.gatekeeper import db
from ossbot.gatekeeper.telegram.actions import Tap, action
from ossbot.gatekeeper.telegram.commands.registry import COMMANDS, CommandContext
from ossbot.gatekeeper.telegram.runtime import ui_of


class QuietArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: int = Field(ge=1, le=24)


class SecondTapArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern=r"^kill$")


def _cctx(tap: Tap, args: list[str] | None = None) -> CommandContext:
    return CommandContext(tap.app, tap.user_id, args or [], thread_id=tap.thread_id, message_id=tap.message_id,
                          handlers=tap.handlers)  # fmt: skip


async def _run(tap: Tap, name: str, args: list[str] | None = None) -> str:
    spec = COMMANDS.get(name)
    if spec is None:
        return "Not available yet."
    reply = await spec.func(_cctx(tap, args), args or [])
    tap.handlers._send_reply(reply, thread_id=tap.thread_id)
    return ""


@action("dash_refresh")
async def refresh(tap: Tap) -> str:
    ui_of(tap.app).dashboard.tick(force=True)
    return "Refreshed."


@action("dash_queue")
async def queue(tap: Tap) -> str:
    return await _run(tap, "queue")


@action("dash_pause")
async def pause(tap: Tap) -> str:
    tap.app.control.pause("telegram dashboard", tap.actor)
    tap.handlers.cancel(tap.handlers.KILL_CANCELS)
    ui_of(tap.app).dashboard.tick(force=True)
    return "⏸ Paused."


@action("dash_resume")
async def resume(tap: Tap) -> str:
    return await _run(tap, "resume")


@action("quiet_set", arg=QuietArg)
async def quiet(tap: Tap) -> str:
    prefs = ui_of(tap.app).prefs
    until = tap.app.clock.now() + timedelta(hours=int(tap.arg.hours))
    prefs.set("quiet_until", to_iso(until))
    with db.transaction(tap.app.conn):
        tap.app.audit.append(tap.actor, "quiet_set", {"until": to_iso(until)})
    return f"🌙 Quiet for {tap.arg.hours} h (P0 alerts still ring)."


@action("dash_down")
async def down(tap: Tap) -> str:
    return await _run(tap, "down")


@action("dash_kill")
async def kill_first(tap: Tap) -> str:
    ui_of(tap.app).dashboard.arm_kill()
    return "Tap 🛑 again within 10 s to kill."


@action("danger_second_tap", arg=SecondTapArg)
async def kill_second(tap: Tap) -> str:
    tap.app.control.kill("telegram dashboard", tap.actor, "soft")
    tap.handlers.cancel(tap.handlers.KILL_CANCELS)
    ui_of(tap.app).dashboard.tick(force=True)
    return "🛑 Killed. /resume to continue."
