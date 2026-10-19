"""Permission answers (DESIGN §15.2): reject_once > reject_always > cancelled, never synthesized
options, never ``_meta.kiro.consent``."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.acp.conftest import FakeRun, ask, client, v3_core

ALL = [
    {"optionId": "accept", "kind": "allow_once"},
    {"optionId": "always-accept", "kind": "allow_always"},
    {"optionId": "reject", "kind": "reject_once"},
    {"optionId": "always-reject", "kind": "reject_always"},
]


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (ALL, {"outcome": {"outcome": "selected", "optionId": "reject"}}),
        (ALL[:2] + ALL[3:], {"outcome": {"outcome": "selected", "optionId": "always-reject"}}),
        (ALL[:2], {"outcome": {"outcome": "cancelled"}}),
        ([], {"outcome": {"outcome": "cancelled"}}),
        ([{"optionId": 7, "kind": "reject_once"}], {"outcome": {"outcome": "cancelled"}}),
    ],
)
def test_permission_response(options: list[object], expected: dict[str, object]) -> None:
    assert v3_core.permission_response({"options": options}) == expected


def test_permission_round_trip_never_returns_consent(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    run = fake(prompts=[[{"permission": ALL}, {"permission": ALL[:2]}, {"text": "denied"}]])
    c = client(run, tmp_path)
    assert ask(c).choices[0].message.content == "denied"
    answers = [f["result"] for f in run.responses() if "result" in f]
    assert answers == [
        {"outcome": {"outcome": "selected", "optionId": "reject"}},
        {"outcome": {"outcome": "cancelled"}},
    ]
    assert all("_meta" not in a for a in answers)
    c.close()
