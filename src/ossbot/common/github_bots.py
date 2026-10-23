"""Bot classification of GitHub users, in code, never by a model (DESIGN §9.12, §14.11.11).

A user is a bot when GitHub says so (``user.type == "Bot"``) or its login ends in ``[bot]`` (GitHub Apps
such as ``coderabbitai[bot]``). The runner's watcher drops bot items before the watch decision; the
gatekeeper's status card turns top-level bot inline comments into finding cards. Depends on no component.
"""

from __future__ import annotations

from collections.abc import Mapping

BOT_SUFFIX = "[bot]"


def is_bot(user: object) -> bool:
    """``user`` is a GitHub user object (a mapping); anything else is not a bot."""
    if not isinstance(user, Mapping):
        return False
    if user.get("type") == "Bot":
        return True
    login = user.get("login")
    return isinstance(login, str) and login.lower().endswith(BOT_SUFFIX)


def is_owner(user: object, login: str) -> bool:
    """The owner's own account (case-insensitive login match; an empty ``login`` matches nobody)."""
    if not isinstance(user, Mapping) or not login:
        return False
    who = user.get("login")
    return isinstance(who, str) and who.lower() == login.lower()
