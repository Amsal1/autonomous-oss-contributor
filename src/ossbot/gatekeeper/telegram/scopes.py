"""Menu button, command scopes and the short description (DESIGN §14.10.2, §14.10.3, D2, AC 13, AC 33).

* ``setChatMenuButton(owner, MenuButtonCommands)``.
* ``setMyCommands`` for ``BotCommandScopeChat(owner)`` from ``registry.menu(state)``; the default scope
  is set to the empty list, so strangers see no commands.
* The owner's list is re-set whenever the control state changes (running, paused, killed, draining,
  down (workers)); killed mode keeps only ``/status /resume /logs /help /start``.
* The short description is "🟢 Online" except after a full (graceful) down, which sets
  "💤 Offline since <HH:MM> — wake the laptop to reconnect"; a workers-only down leaves it online.

Failures are WARNINGs and retried at the next state change (§14.20.5).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ossbot.gatekeeper.outbox import SendError
from ossbot.gatekeeper.telegram.commands.registry import MenuState, menu
from ossbot.gatekeeper.telegram.prefs import UxPrefs

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

log = logging.getLogger(__name__)
Call = Callable[[str, dict[str, Any]], Awaitable[Any]]
ONLINE = "🟢 Online"


def offline_description(hhmm: str) -> str:
    return f"💤 Offline since {hhmm} — wake the laptop to reconnect"


def menu_state(ctx: AppContext) -> MenuState:
    view = ctx.control.view()
    if view.state in {"killed", "paused"}:
        return view.state
    info = ctx.control.lifecycle()
    if info is None:
        return "running"
    return "draining" if info.state == "draining" else "down"


class Scopes:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.applied: MenuState | None = None
        self.description: str | None = None

    async def apply(self, call: Call, state: MenuState | None = None) -> bool:
        """(Re)set the menus for ``state``; True when every call succeeded."""
        state = state or menu_state(self.ctx)
        owner = self.ctx.telegram_chat_id
        ok = True
        calls: list[tuple[str, dict[str, Any]]] = []
        if self.applied is None:
            calls += [
                ("setMyCommands", {"commands": [], "scope": {"type": "default"}}),
                ("setChatMenuButton", {"chat_id": owner, "menu_button": {"type": "commands"}}),
            ]
        calls.append(("setMyCommands", {"commands": menu(state), "scope": {"type": "chat", "chat_id": owner}}))
        for method, payload in calls:
            try:
                await call(method, payload)
            except SendError as exc:
                ok = False
                log.warning("%s failed: %s", method, type(exc).__name__, extra={"event": "telegram_menu_failed"})
        if ok:
            self.applied = state
        return ok

    async def maybe_apply(self, call: Call) -> bool:
        state = menu_state(self.ctx)
        if state == self.applied:
            return False
        return await self.apply(call, state)

    async def set_description(self, call: Call, text: str) -> bool:
        if text == self.description:
            return False
        try:
            await call("setMyShortDescription", {"short_description": text[:100]})
        except SendError as exc:
            log.warning("setMyShortDescription failed: %s", type(exc).__name__,
                        extra={"event": "telegram_menu_failed"})  # fmt: skip
            return False
        self.description = text
        # persisted: the next start tells a graceful down ("💤 Offline …") from a sleep (§14.16)
        UxPrefs(self.ctx.conn, self.ctx.clock).set("short_description", text)
        return True
