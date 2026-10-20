"""Command registry (DESIGN §14.5 mechanism, §14.14.1 command set, §14.10.2 menus). Built-ins plus
entry points of group ``ossbot.telegram_commands``.

``safety``: ``read``, ``stop`` and ``personal`` act immediately; ``go`` and ``config`` always reply
with a confirm button (a message-bound, single-use, 5-minute token) and act only on that tap.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

log = logging.getLogger(__name__)
# read: immediate, no change; stop: immediate (stopping is always safe); go/config: confirm tap first;
# personal: presentation-only owner preferences (quiet, notify, rich views), immediate (§14.14.1);
# tune: autonomous-discovery preferences (/size, /filters), immediate + 60 s Undo, never the ceiling (U13);
# request: /work, creates a scouting item only (both gates still apply)
Safety = Literal["read", "stop", "go", "config", "personal", "tune", "request"]
ENTRY_POINT_GROUP = "ossbot.telegram_commands"


@dataclass(frozen=True)
class Confirm:
    """A confirm button: ``purpose`` names the confirm handler, ``arg`` travels in the token row."""

    purpose: str
    label: str
    arg: str | None = None
    # §14.14.2: danger for a go/config confirm that widens anything, primary otherwise
    style: Literal["primary", "danger", "success"] = "primary"


@dataclass(frozen=True)
class Reply:
    text: str  # HTML, already escaped
    confirm: Confirm | None = None
    view: Any = None  # a ui.kit.View (pickers, pagers); replaces ``text`` when set
    document: tuple[str, bytes] | None = None  # (file name, bytes) sent after the reply (/logs)
    deferred: bool = False  # a spawned task sends the reply itself (e.g. /work after its GitHub read)


@dataclass(frozen=True)
class CommandContext:
    app: AppContext
    tg_user_id: int
    args: list[str] = field(default_factory=list)
    thread_id: int | None = None  # the originating topic: replies and pickers go back there (§14.10.1)
    message_id: int | None = None
    stale_s: int = 0  # > 0: the message waited this long (laptop offline, §14.10.4 "Stale messages")
    handlers: Any = None  # telegram.handlers.Handlers (spawn/cancel), None in API-side tests

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


MenuState = Literal["running", "paused", "killed", "draining", "down"]
# DESIGN §14.10.2 (D2, AC 13): killed mode keeps only these; /settings is hidden while killed.
KILLED_MENU: tuple[str, ...] = ("status", "resume", "logs", "help", "start")
GLOBAL_COMMANDS: tuple[str, ...] = ("start", "help", "settings")  # always present outside killed mode
# shown only in the states that need them
CONDITIONAL: dict[str, frozenset[str]] = {
    "up": frozenset({"down"}),
    "undrain": frozenset({"draining"}),
    "resume": frozenset({"paused", "killed"}),
}
MENU_DESCRIPTION_MAX = 60  # §14.17.4: plain, verb first


def menu(state: MenuState = "running") -> list[dict[str, str]]:
    """``setMyCommands`` payload for the owner scope, generated from the registry for one control state."""
    if state == "killed":
        names = [n for n in KILLED_MENU if n in COMMANDS]
    else:
        names = [n for n in sorted(COMMANDS) if state in CONDITIONAL.get(n, frozenset({state}))]
        names += [n for n in GLOBAL_COMMANDS if n in COMMANDS and n not in names]
    return [{"command": n, "description": COMMANDS[n].help[:MENU_DESCRIPTION_MAX]} for n in names]
