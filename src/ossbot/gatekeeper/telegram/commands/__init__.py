"""Telegram command registry and the built-in commands (DESIGN §14.5, §14.14.1)."""

from __future__ import annotations

import importlib

# Modules that register commands, confirm handlers, kit actions or input handlers on import.
BUILTIN_MODULES: tuple[str, ...] = (
    "ossbot.gatekeeper.telegram.commands.builtin",
    "ossbot.gatekeeper.telegram.dashboard_actions",
    "ossbot.gatekeeper.telegram.commands.review",
    "ossbot.gatekeeper.telegram.review.actions",
    "ossbot.gatekeeper.telegram.review.edit_text",
    "ossbot.gatekeeper.telegram.proposal_actions",
    "ossbot.gatekeeper.telegram.rescope_cards",
    "ossbot.gatekeeper.telegram.work_links",
    "ossbot.gatekeeper.telegram.size_pickers",
    "ossbot.gatekeeper.telegram.commands.common",
    "ossbot.gatekeeper.telegram.commands.lifecycle",
    "ossbot.gatekeeper.telegram.commands.admin",
    "ossbot.gatekeeper.telegram.commands.personal",
    "ossbot.gatekeeper.telegram.chat_actions",
)


def load_builtin_modules() -> None:
    for name in BUILTIN_MODULES:
        importlib.import_module(name)
