"""The Telegram workspace state of one gatekeeper process (DESIGN §14.10): topics, scopes, dashboard,
ForceReply prompts and UX preferences. ``ui_of(ctx)`` returns the one instance (created on first use)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ossbot.gatekeeper.telegram.dashboard import Dashboard
from ossbot.gatekeeper.telegram.pending_input import PendingInputs
from ossbot.gatekeeper.telegram.prefs import UxPrefs
from ossbot.gatekeeper.telegram.scopes import Scopes
from ossbot.gatekeeper.telegram.topics import Topics

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext


@dataclass
class TelegramUi:
    topics: Topics
    scopes: Scopes
    dashboard: Dashboard
    pending: PendingInputs
    prefs: UxPrefs


def ui_of(ctx: AppContext) -> TelegramUi:
    cached = ctx.__dict__.get("_telegram_ui")
    if isinstance(cached, TelegramUi):
        return cached
    ui = TelegramUi(Topics(ctx), Scopes(ctx), Dashboard(ctx), PendingInputs(ctx.conn, ctx.clock),
                    UxPrefs(ctx.conn, ctx.clock))  # fmt: skip
    ctx.__dict__["_telegram_ui"] = ui
    return ui
