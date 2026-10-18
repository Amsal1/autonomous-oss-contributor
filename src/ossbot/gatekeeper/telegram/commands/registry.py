"""Command registry (DESIGN §14.5). Built-ins plus entry points of group ``ossbot.telegram_commands``.

``safety``: ``read`` and ``stop`` act immediately; ``go`` and ``config`` always reply with a
confirm button (a message-bound, single-use, 5-minute token) and act only on that tap.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

log = logging.getLogger(__name__)
Safety = Literal["read", "stop", "go", "config"]
ENTRY_POINT_GROUP = "ossbot.telegram_commands"


@dataclass(frozen=True)
class Confirm:
    """A confirm button: ``purpose`` names the confirm handler, ``arg`` travels in the token row."""

    purpose: str
    label: str
    arg: str | None = None


@dataclass(frozen=True)
class Reply:
    text: str  # HTML, already escaped
    confirm: Confirm | None = None


@dataclass(frozen=True)
class CommandContext:
    app: AppContext
    tg_user_id: int
    args: list[str] = field(default_factory=list)

    @property
    def actor(self) -> str:
        return f"tg:{self.tg_user_id}"


Handler = Callable[[CommandContext, list[str]], Awaitable[Reply]]
ConfirmHandler = Callable[[CommandContext, str | None], Awaitable[Reply]]
# DESIGN §14.18.5, BA-18: "local" runs inline in the update slot (SQLite and local reads only); "io"
# awaits I/O beyond that (root helpers, lifecycle, the Hermes probe, the proxy renderer, /model) and
# is spawned as a ``command`` task so it never holds the slot.
Effect = Literal["local", "io"]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    help: str
    safety: Safety
    func: Handler
    effect: Effect = "local"


COMMANDS: dict[str, CommandSpec] = {}
CONFIRMS: dict[str, ConfirmHandler] = {}
CONFIRM_EFFECTS: dict[str, Effect] = {}


def command(name: str, *, help: str, safety: Safety, effect: Effect = "local") -> Callable[[Handler], Handler]:
    def register(func: Handler) -> Handler:
        if name in COMMANDS:
            raise ValueError(f"command /{name} registered twice")
        COMMANDS[name] = CommandSpec(name, help, safety, func, effect)
        return func

    return register


def confirm_handler(purpose: str, *, effect: Effect = "local") -> Callable[[ConfirmHandler], ConfirmHandler]:
    def register(func: ConfirmHandler) -> ConfirmHandler:
        CONFIRMS[purpose] = func
        CONFIRM_EFFECTS[purpose] = effect
        return func

    return register


def load_plugins() -> list[str]:
    """Import every ``ossbot.telegram_commands`` entry point (they register on import)."""
    loaded = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            ep.load()
            loaded.append(ep.name)
        except Exception:
            log.exception("telegram command plugin %s failed to load", ep.name)
    return loaded


def menu() -> list[dict[str, str]]:
    """``setMyCommands`` payload, generated from the registry."""
    return [
        {"command": spec.name, "description": spec.help[:256]}
        for spec in sorted(COMMANDS.values(), key=lambda s: s.name)
    ]
