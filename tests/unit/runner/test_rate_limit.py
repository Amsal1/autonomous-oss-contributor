"""``python -m ossbot.runner.rate_limit`` (discovery fix 6): the runner's core and search buckets from one
``GET /rate_limit`` as one JSON line of integers; a failed read prints only an error class and exits 1;
the token never appears in the output."""

from __future__ import annotations

import json

import pytest

from ossbot.runner import rate_limit
from ossbot.runner.github_read import GitHubRead
from tests.fakes.fake_github import FakeGitHub

TOKEN = "github_pat_" + "R" * 30


def read(fake: FakeGitHub) -> GitHubRead:
    return GitHubRead(fake.sync_client(TOKEN), sleep=lambda s: None)


def test_main_prints_the_core_and_search_buckets(capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeGitHub()
    fake.rate_limits["search"] = {"limit": 10, "used": 3, "remaining": 7, "reset": 1_790_000_030}
    assert rate_limit.main(read(fake)) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {
        "core": {"limit": 5000, "used": 790, "remaining": 4210, "reset": 1_790_003_600},
        "search": {"limit": 10, "used": 3, "remaining": 7, "reset": 1_790_000_030},
    }
    assert TOKEN not in out and "x-ratelimit" not in out
    assert [(r.method, r.path) for r in fake.requests] == [("GET", "/rate_limit")]


def test_non_integers_become_null(capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeGitHub()
    doc = {"resources": {"core": {"limit": "5000", "used": True, "remaining": 1, "reset": 2}, "search": "junk"}}
    fake.answers["GET /rate_limit"] = [(200, {}, doc)]
    assert rate_limit.main(read(fake)) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["core"] == {"limit": None, "used": None, "remaining": 1, "reset": 2}
    assert got["search"] == {"limit": None, "used": None, "remaining": None, "reset": None}


def test_a_5xx_prints_the_error_class_and_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeGitHub()
    fake.failures["GET /rate_limit"] = [502, 502, 502, 502]
    assert rate_limit.main(read(fake)) == 1
    out = capsys.readouterr().out
    assert json.loads(out) == {"error": "github_unavailable"} and TOKEN not in out


def test_a_rate_limit_answer_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeGitHub()
    fake.answers["GET /rate_limit"] = [(429, {"retry-after": "30"}, {"message": "slow down"})]
    assert rate_limit.main(read(fake)) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "github_rate_limited"}
