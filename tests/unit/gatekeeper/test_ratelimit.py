"""Rate limits (DESIGN §12.6, AC 11, M10): rolling 24 h windows, executing counts, dry-run not counted."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx

from ossbot.common.clock import FakeClock, to_iso
from ossbot.gatekeeper import ratelimit
from ossbot.gatekeeper.context import AppContext
from tests.conftest import insert_action, insert_contribution

GH = "https://api.github.test"


def executed(ctx: AppContext, kind: str, status: str, ago: timedelta, upstream: str = "langfuse/langfuse") -> None:
    cid = insert_contribution(ctx, upstream=upstream)
    insert_action(ctx, cid, kind, {}, status=status, executed_at=to_iso(ctx.clock.now() - ago))  # type: ignore[arg-type]


async def no_open_prs(repo: str) -> int:
    return 0


async def one_open_pr(repo: str) -> int:
    return 1


@pytest.mark.parametrize(
    ("ago", "counts"),
    [(timedelta(hours=23, minutes=59), True), (timedelta(hours=24), False), (timedelta(hours=25), False)],
)
async def test_rolling_window_edges(ctx: AppContext, ago: timedelta, counts: bool) -> None:
    executed(ctx, "comment", "done", ago)
    assert ratelimit.used_in_window(ctx.conn, ctx.clock, "comment") == (1 if counts else 0)


@pytest.mark.parametrize(("status", "counts"), [("done", True), ("executing", True), ("dry_run_done", False),
                                                ("failed", False), ("cancelled", False)])  # fmt: skip
async def test_statuses_counted(ctx: AppContext, status: str, counts: bool) -> None:
    executed(ctx, "open_pr", status, timedelta(hours=1))
    assert ratelimit.used_in_window(ctx.conn, ctx.clock, "open_pr") == (1 if counts else 0)


async def test_prs_per_24h(ctx: AppContext) -> None:
    limits = ctx.config.policy.limits
    executed(ctx, "open_pr", "done", timedelta(hours=1), "a/one")
    d = await ratelimit.check(ctx.conn, ctx.clock, limits, "open_pr", "b/two", no_open_prs)
    assert d.allowed
    executed(ctx, "open_pr", "executing", timedelta(minutes=1), "a/one")
    d = await ratelimit.check(ctx.conn, ctx.clock, limits, "open_pr", "b/two", no_open_prs)
    assert not d.allowed and d.code == "rate_limited:prs_per_24h"


async def test_open_prs_per_repo_live_count(ctx: AppContext) -> None:
    limits = ctx.config.policy.limits
    d = await ratelimit.check(ctx.conn, ctx.clock, limits, "open_pr", "langfuse/langfuse", one_open_pr)
    assert not d.allowed and d.counter == "open_prs_per_repo"
    assert (await ratelimit.check(ctx.conn, ctx.clock, limits, "open_pr", "langfuse/langfuse", no_open_prs)).allowed


async def test_open_pr_without_counter_fails_closed(ctx: AppContext) -> None:
    assert not (await ratelimit.check(ctx.conn, ctx.clock, ctx.config.policy.limits, "open_pr", "a/b")).allowed


async def test_executing_open_pr_counts_for_repo(ctx: AppContext) -> None:
    executed(ctx, "open_pr", "executing", timedelta(minutes=1), "langfuse/langfuse")
    d = await ratelimit.check(
        ctx.conn, ctx.clock, ctx.config.policy.limits, "open_pr", "langfuse/langfuse", no_open_prs
    )
    assert not d.allowed and d.counter == "open_prs_per_repo"


async def test_comment_and_claim_limits(ctx: AppContext) -> None:
    limits = ctx.config.policy.limits
    for _ in range(limits.comments_per_24h - 1):
        executed(ctx, "comment", "done", timedelta(hours=2))
    assert (await ratelimit.check(ctx.conn, ctx.clock, limits, "comment", "a/b")).allowed
    executed(ctx, "comment", "done", timedelta(hours=2))
    assert not (await ratelimit.check(ctx.conn, ctx.clock, limits, "comment", "a/b")).allowed
    for _ in range(limits.issue_claims_per_24h):
        executed(ctx, "issue_claim", "done", timedelta(hours=2))
    assert not (await ratelimit.check(ctx.conn, ctx.clock, limits, "issue_claim", "a/b")).allowed


async def test_comment_on_own_pr_allowed_at_open_pr_limit(ctx: AppContext) -> None:
    executed(ctx, "open_pr", "done", timedelta(hours=1))
    d = await ratelimit.check(
        ctx.conn, ctx.clock, ctx.config.policy.limits, "comment", "langfuse/langfuse", one_open_pr
    )
    assert d.allowed


async def test_push_update_consumes_nothing(ctx: AppContext) -> None:
    for _ in range(5):
        executed(ctx, "open_pr", "done", timedelta(hours=1))
    d = await ratelimit.check(
        ctx.conn, ctx.clock, ctx.config.policy.limits, "push_update", "langfuse/langfuse", one_open_pr
    )
    assert d.allowed and d.counter is None


async def test_window_slides(ctx: AppContext, clock: FakeClock) -> None:
    limits = ctx.config.policy.limits
    executed(ctx, "open_pr", "done", timedelta(hours=1))
    executed(ctx, "open_pr", "done", timedelta(hours=2))
    assert not (await ratelimit.check(ctx.conn, ctx.clock, limits, "open_pr", "x/y", no_open_prs)).allowed
    clock.advance(hours=23)
    assert (await ratelimit.check(ctx.conn, ctx.clock, limits, "open_pr", "x/y", no_open_prs)).allowed


@respx.mock
async def test_github_open_pr_counter() -> None:
    route = respx.get(f"{GH}/search/issues").mock(return_value=httpx.Response(200, json={"total_count": 1}))
    async with httpx.AsyncClient(base_url=GH) as client:
        count = ratelimit.github_open_pr_counter(client, "test-bot-login")
        assert await count("langfuse/langfuse") == 1
    assert "author:test-bot-login" in route.calls[0].request.url.params["q"]
    assert "repo:langfuse/langfuse" in route.calls[0].request.url.params["q"]


@respx.mock
async def test_github_counter_errors_propagate(ctx: AppContext) -> None:
    respx.get(f"{GH}/search/issues").mock(return_value=httpx.Response(502))
    async with httpx.AsyncClient(base_url=GH) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await ratelimit.check(ctx.conn, ctx.clock, ctx.config.policy.limits, "open_pr", "a/b",
                                  ratelimit.github_open_pr_counter(client, "x"))  # fmt: skip
