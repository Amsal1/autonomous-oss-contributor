"""Handlers for typed input (DESIGN §14.10.4 rules 3 and 5): ForceReply answers by ``pending_input``
kind, and replies to gatekeeper views by view family. Feature modules register here; the handler
module only routes. Every handler treats the text as untrusted (``undefuse`` first, then the same
checks as the matching button path)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ossbot.gatekeeper.telegram.pending_input import InputKind, Prompt

if TYPE_CHECKING:
    import sqlite3

    from ossbot.gatekeeper.context import AppContext

ViewFamily = Literal["review", "proposal", "skill", "links"]  # "links": routing rule 6 (link cards)
VIEW_FAMILY: dict[str, ViewFamily] = {
    "file": "review", "explorer": "review", "interdiff": "review", "card": "review", "body_part": "review",
    "commit_msg": "review", "proposal": "proposal", "skill_card": "skill", "skill_part": "skill",
}  # fmt: skip


@dataclass
class Typed:
    app: AppContext
    handlers: Any
    text: str  # undefused
    user_id: int
    chat_id: int
    message_id: int
    thread_id: int | None
    quote: str | None = None  # the user's quote of the replied-to message (Bot API TextQuote)
    document: tuple[str, str | None, int] | None = None  # (file_id, mime, size) of an attached document
    prompt: Prompt | None = None
    view: sqlite3.Row | None = None
    route: Any = None  # routing.Route (link cards: its links and notes)

    @property
    def actor(self) -> str:
        return f"tg:{self.user_id}"


TypedHandler = Callable[[Typed], Awaitable[None]]
INPUTS: dict[str, TypedHandler] = {}
VIEW_REPLIES: dict[str, TypedHandler] = {}


def input_handler(kind: InputKind) -> Callable[[TypedHandler], TypedHandler]:
    def register(func: TypedHandler) -> TypedHandler:
        INPUTS[kind] = func
        return func

    return register


def view_reply(family: ViewFamily) -> Callable[[TypedHandler], TypedHandler]:
    def register(func: TypedHandler) -> TypedHandler:
        VIEW_REPLIES[family] = func
        return func

    return register
