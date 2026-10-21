"""Tappable command shortcuts (post-v1 plan D9, DESIGN §14.10.4, ADR-0020 addendum).

A command typed in "All" makes Telegram open an implicit topic; an inline button sends no message, so
it never does. With ``telegram.yaml command_shortcuts: true`` the dashboard (pinned and its /status
copies), the /help menu and button-less command replies in All carry these buttons. Each ``sc_*``
action runs the registered command exactly as typing it would (the same function, the same killed-mode
rule) and sends the reply into the tapped message's thread. Queue, Pause and Resume reuse the dashboard
actions (``dash_queue``, ``dash_pause`` immediate, ``dash_resume`` opens its confirm). Kill is not here:
it stays a two-tap button on the dashboard cards only (D14). Presentation only: nothing here approves,
publishes or changes a limit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from ossbot.gatekeeper.telegram.actions import Tap, action
from ossbot.gatekeeper.telegram.commands.lifecycle import killed_view
from ossbot.gatekeeper.telegram.commands.registry import COMMANDS, CommandContext, Reply
from ossbot.gatekeeper.telegram.runtime import ui_of
from ossbot.gatekeeper.telegram.ui import kit as k

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

RowsKind = Literal["help", "compact"]
# replies that are already the menu or the card the compact row would lead to
NO_COMPACT = frozenset({"status", "help"})


def enabled(ctx: AppContext) -> bool:
    return ctx.config.telegram.command_shortcuts


def shortcut_rows(ctx: AppContext, kind: RowsKind) -> k.Rows:
    """Fresh buttons (minted and bound to the message at send, ``ui.send.deliver``); [] when off."""
    if not enabled(ctx):
        return []
    status, queue = k.button("🚦 Status", "sc_status"), k.button("📥 Queue", "dash_queue")
    if kind == "compact":
        return [[status, queue, k.button("❓ Help", "sc_help")]]
    return [
        [status, queue, k.button("🛠 Work", "sc_work")],
        [k.button("📏 Size", "sc_size"), k.button("🔎 Filters", "sc_filters"), k.button("🤖 Model", "sc_model")],
        [k.button("⏸ Pause", "dash_pause"), k.button("▶️ Resume…", "dash_resume")],
    ]


def compact_in_all(ctx: AppContext, name: str, *, thread_id: int | None, typed_in_all: bool) -> bool:
    """Whether a reply to command ``name`` gets the compact row: shortcuts on, topics mode, the reply
    goes to All (``thread_id`` None) because the command came from All, and it is not /status or /help."""
    if not enabled(ctx) or name in NO_COMPACT or not typed_in_all or thread_id is not None:
        return False
    return ui_of(ctx).topics.mode == "topics"


async def _run(tap: Tap, name: str) -> str:
    """Run ``/name`` (no arguments) like the typed command, replying into the tapped message's thread."""
    from ossbot.gatekeeper.telegram.handlers import KILLED_ALLOWED, KILLED_TEXT  # noqa: PLC0415 - imports us

    spec = COMMANDS.get(name)
    if spec is None:
        return "Not available yet."
    if tap.app.control.view().state == "killed" and name not in KILLED_ALLOWED:
        reply = Reply(KILLED_TEXT, view=killed_view(tap.app))  # §14.16, as for the typed command
    else:
        cctx = CommandContext(tap.app, tap.user_id, [], thread_id=tap.thread_id, message_id=tap.message_id,
                              handlers=tap.handlers)  # fmt: skip
        reply = await spec.func(cctx, [])
    compact = compact_in_all(tap.app, name, thread_id=tap.thread_id, typed_in_all=True)
    tap.handlers._send_reply(reply, thread_id=tap.thread_id, compact=compact)
    return ""


@action("sc_status")
async def sc_status(tap: Tap) -> str:
    return await _run(tap, "status")


@action("sc_work")
async def sc_work(tap: Tap) -> str:
    return await _run(tap, "work")


@action("sc_size")
async def sc_size(tap: Tap) -> str:
    return await _run(tap, "size")


@action("sc_filters")
async def sc_filters(tap: Tap) -> str:
    return await _run(tap, "filters")


@action("sc_model")
async def sc_model(tap: Tap) -> str:
    return await _run(tap, "model")


@action("sc_help")
async def sc_help(tap: Tap) -> str:
    return await _run(tap, "help")
