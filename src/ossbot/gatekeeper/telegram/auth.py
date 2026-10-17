"""Update authorization (DESIGN §14.2, AC 7).

An update is accepted only from ``profile.telegram.user_id`` in its private chat (chat id equals
the user id; value from the active config snapshot). Everything else gets no reply (callbacks an
empty ``answerCallbackQuery``), a WARNING with the user id only (never the text) and a counter.
"""

from __future__ import annotations

import logging
from collections import Counter

from telegram import Update

log = logging.getLogger(__name__)
REJECTED: Counter[str] = Counter()


def authorized(update: Update, user_id: int) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    ok = (
        user_id != 0
        and user is not None
        and chat is not None
        and user.id == user_id
        and chat.type == "private"
        and chat.id == user_id
    )
    if not ok:
        REJECTED["unauthorized"] += 1
        log.warning(
            "telegram update rejected: user %s chat %s (%s)",
            user.id if user else None,
            chat.id if chat else None,
            chat.type if chat else None,
            extra={"event": "telegram_unauthorized"},
        )
    return ok
