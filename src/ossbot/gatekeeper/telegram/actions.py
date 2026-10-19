"""Kit token handlers (DESIGN §14.18.2): one registry for every non-approval button purpose.

``@action(purpose, arg=Model, effect=...)`` registers a handler. The callback path checks the token
first (``ApprovalService.use_token``: owner, message-bound, unexpired, consumed only when single-use),
then validates ``arg`` with the purpose's pydantic model (``extra="forbid"``; a bad arg is an invalid
token, §14.20.6), then runs the handler. ``local`` handlers run inline (SQLite and outbox only);
``io`` handlers are spawned as ``command`` tasks (§14.18.5). A handler returns the toast text.

Decision purposes (approve, confirm, proposal_start, skill_approve, ...) are NOT here: they stay on the
reviewed paths in ``handlers.py`` and the decision services (U3).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

Effect = Literal["local", "io"]


class NoArg(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class Tap:
    app: AppContext
    handlers: Any  # telegram.handlers.Handlers (spawn, cancel)
    token: str
    row: sqlite3.Row
    arg: Any  # the validated arg model
    user_id: int
    chat_id: int
    message_id: int
    thread_id: int | None = None

    @property
    def actor(self) -> str:
        return f"tg:{self.user_id}"


Handler = Callable[[Tap], Awaitable[str]]


@dataclass(frozen=True)
class ActionSpec:
    purpose: str
    func: Handler
    arg: type[BaseModel]
    effect: Effect


ACTIONS: dict[str, ActionSpec] = {}


def action(purpose: str, *, arg: type[BaseModel] = NoArg, effect: Effect = "local") -> Callable[[Handler], Handler]:
    def register(func: Handler) -> Handler:
        if purpose in ACTIONS:
            raise ValueError(f"action {purpose} registered twice")
        ACTIONS[purpose] = ActionSpec(purpose, func, arg, effect)
        return func

    return register


class BadArg(ValueError):
    pass


def parse_arg(spec: ActionSpec, raw: str | None) -> BaseModel:
    try:
        data = json.loads(raw) if raw else {}
    except ValueError as exc:
        raise BadArg("arg is not JSON") from exc
    if not isinstance(data, dict):
        raise BadArg("arg is not an object")
    try:
        return spec.arg.model_validate(data)
    except ValidationError as exc:
        raise BadArg(exc.errors()[0]["msg"]) from exc
