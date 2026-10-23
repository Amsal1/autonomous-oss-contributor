"""The gatekeeper reader's errors carry GitHub's rate-limit reset (the status card honours it)."""

from __future__ import annotations

import httpx
import pytest

from ossbot.gatekeeper.github_read import GitHubError, GitHubReader, raise_for
from tests.fakes.fake_github import FakeGitHub


def _err(status: int, headers: dict[str, str] | None = None) -> GitHubError:
    with pytest.raises(GitHubError) as info:
        raise_for(httpx.Response(status, headers=headers or {}), "GET /x")
    return info.value


def test_primary_limit_sets_the_reset_epoch() -> None:
    exc = _err(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1790000000"})
    assert exc.reset_epoch == 1790000000.0 and exc.retry_after_s is None and exc.auth is True


def test_retry_after_is_kept() -> None:
    exc = _err(429, {"retry-after": "60"})
    assert exc.retry_after_s == 60.0 and exc.reset_epoch is None and not exc.transient


def test_plain_403_has_no_limit() -> None:
    exc = _err(403, {"x-ratelimit-remaining": "12", "x-ratelimit-reset": "1790000000"})
    assert exc.reset_epoch is None and exc.retry_after_s is None and exc.auth is True


@pytest.mark.parametrize("value", ["soon", "nan", "inf"])
def test_bad_header_values_are_ignored(value: str) -> None:
    exc = _err(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": value, "retry-after": value})
    assert exc.reset_epoch is None and exc.retry_after_s is None


def test_limit_headers_only_on_403_and_429() -> None:
    exc = _err(404, {"retry-after": "60", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "1"})
    assert exc.reset_epoch is None and exc.retry_after_s is None


async def test_review_comments_and_one_comment_are_gets() -> None:
    """The status card's bot findings and the Fix-this re-fetch (owner decision 2026-10-23 (b))."""
    fake = FakeGitHub()
    comment = {"id": 7, "path": "src/x.ts", "line": 120, "body": "b", "user": {"login": "coderabbitai[bot]"}}
    fake.review_comments[("o/r", 5)] = [comment]
    reader = GitHubReader(fake.client())
    assert await reader.review_comments("o/r", 5) == [comment]
    assert await reader.review_comment("o/r", 7) == comment
    assert await reader.review_comment("o/r", 8) is None
    assert fake.writes == [] and [(r.method, r.path) for r in fake.requests] == [
        ("GET", "/repos/o/r/pulls/5/comments"), ("GET", "/repos/o/r/pulls/comments/7"),
        ("GET", "/repos/o/r/pulls/comments/8")]  # fmt: skip


def test_server_error_stays_transient() -> None:
    exc = _err(500, {"retry-after": "60"})
    assert exc.transient and exc.reset_epoch is None and exc.retry_after_s is None
