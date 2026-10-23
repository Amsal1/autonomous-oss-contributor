"""Bot classification in code (DESIGN §9.12): ``user.type == "Bot"`` or a ``[bot]`` login."""

from __future__ import annotations

from typing import Any

import pytest

from ossbot.common.github_bots import is_bot, is_owner

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        ({"login": "coderabbitai[bot]", "type": "Bot"}, True),
        ({"login": "coderabbitai[bot]"}, True),  # the suffix alone
        ({"login": "Copilot", "type": "Bot"}, True),  # the type alone
        ({"login": "Renovate[BOT]", "type": "User"}, True),  # mixed case
        ({"login": "maintainer", "type": "User"}, False),
        ({"login": "botanist", "type": "User"}, False),
        ({"login": "bot]"}, False),
        ({}, False),
        ({"login": None, "type": None}, False),
        (None, False),
        ("coderabbitai[bot]", False),  # not a user object
    ],
)
def test_is_bot(user: Any, expected: bool) -> None:
    assert is_bot(user) is expected


def test_is_owner_is_case_insensitive() -> None:
    assert is_owner({"login": "Octo-Owner"}, "octo-owner")
    assert not is_owner({"login": "maintainer"}, "octo-owner")
    assert not is_owner({"login": "octo-owner"}, "")
    assert not is_owner(None, "octo-owner")
    assert not is_owner({"login": 7}, "octo-owner")
