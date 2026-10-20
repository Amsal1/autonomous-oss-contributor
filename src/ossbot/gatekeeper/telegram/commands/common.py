"""Shared pieces of the §14.14 command flows: the command-confirm buttons (§14.14.2), the picker-confirm
panel (the picker tap edits its message into the confirm panel, one message per flow) and ``↩️ Cancel``."""

from __future__ import annotations

from datetime import timedelta

from ossbot.gatekeeper.telegram.actions import NoArg, Tap, action
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send
from ossbot.gatekeeper.telegram.ui.limits import LONG_LABEL, label_len

CONFIRM_TTL = timedelta(minutes=5)  # command confirms: message-bound, single use, 5 minutes (§14.14.1)
CANCELLED = "Cancelled; nothing changed."


def confirm_button(label: str, purpose: str, arg: str | None = None, *, style: k.Style = "primary") -> k.ButtonSpec:
    """A registry confirm (``CONFIRMS[purpose]``) on a kit view; ``arg`` is stored verbatim in the token."""
    return k.ButtonSpec(label, purpose, style=style, single_use=True, ttl=CONFIRM_TTL, raw_arg=arg)


def cancel_button() -> k.ButtonSpec:
    return k.button("↩️ Cancel", "cmd_cancel", single_use=True, ttl=CONFIRM_TTL)


def confirm_panel(lines: list[k.Line], confirm: k.ButtonSpec) -> k.View:
    """``[✅ <verb>] [↩️ Cancel]`` under the sentence saying what will happen and what is invalidated."""
    return k.plain("confirm_prompt", *lines, rows=[[confirm, cancel_button()]])


def picker_rows(options: list[k.ButtonSpec], per_row: int = 3) -> k.Rows:
    """≤ 3 per row (§14.14.1); 2 when a label is long (the §14.17.4 multi-button label budget)."""
    if per_row > 2 and any(label_len(o.label) > LONG_LABEL for o in options):
        per_row = 2
    return [options[i : i + per_row] for i in range(0, len(options), per_row)]


@action("cmd_cancel", arg=NoArg)
async def cancel(tap: Tap) -> str:
    ui_send.edit(tap.app, k.plain("fyi", k.line(CANCELLED)), message_id=tap.message_id)
    return "Cancelled."
