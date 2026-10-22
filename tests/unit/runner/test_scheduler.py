"""The scheduler's discovery cycle across a cut (discovery pacing, ADR-0003 addendum 2026-10-22, fix 4):
a cut full cycle keeps its ``CycleProgress`` and resumes only the unfinished part; progress older than
``interval_hours`` is dropped; a fresh full cycle never starts within 30 min of the previous one; a
gatekeeper 503 is retried after 5 min. Real gatekeeper in-process, FakeGitHub, GET requests only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ossbot.gatekeeper.context import AppContext
from ossbot.runner.github_read import GitHubRead
from ossbot.runner.gk_client import GkError
from ossbot.runner.pipeline import StageContext
from ossbot.runner.scheduler import DISCOVER_GK_RETRY, DISCOVER_RESUME_MIN, FULL_RESCAN_MIN_GAP, Scheduler
from tests.fakes.fake_github import FakeGitHub
from tests.unit.runner.test_discover_open import issue_reads, limit_answer, mention_checked_before_create, searched

SEED = "langfuse/langfuse"
SECOND = "langfuse/langfuse-python"


def iso(ctx: AppContext, days_ago: float) -> str:
    return (ctx.clock.now() - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def bug(github: FakeGitHub, ctx: AppContext, repo: str, number: int) -> None:
    github.add_issue(repo, number, title=f"Crash {number}", body="steps to reproduce", labels=[{"name": "bug"}],
                     updated_at=iso(ctx, 1), created_at=iso(ctx, 20), comments=0)  # fmt: skip


def clocked(sc: StageContext, ctx: AppContext, github: FakeGitHub) -> None:
    """The read client's wall clock follows the test clock (rate-limit resets)."""
    wall = lambda: ctx.clock.now().timestamp()  # noqa: E731
    sc.gh = GitHubRead(github.sync_client(), sleep=lambda s: None, wall=wall, monotonic=wall)


def scheduler(sc: StageContext, ctx: AppContext) -> Scheduler:
    """Ready (M11 done); ``tick`` runs discovery only: the items it creates are not dispatched here."""
    s = Scheduler(sc, synchronous=True, now=ctx.clock.now)
    s.first_start_done = True
    s.last_catalog = ctx.clock.now()
    s._dispatch = lambda out: None  # type: ignore[method-assign]
    return s


def advance(ctx: AppContext, delta: timedelta) -> None:
    ctx.clock.advance(seconds=delta.total_seconds())  # type: ignore[attr-defined]


def upstreams(sc: StageContext, cids: list[str]) -> list[str]:
    return [str(sc.gk.contribution(c)["upstream"]) for c in cids]


def near(a: datetime | None, b: datetime) -> bool:
    return a is not None and abs((a - b).total_seconds()) < 1


def test_a_cut_at_the_merge_resumes_without_rereading(sc: StageContext, ctx: AppContext, github: FakeGitHub,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    """A 600 s search limit on the seed pick's mention check cuts the cycle; after the reset the re-run
    reads no seed issues and searches no repos again, and creates the pick after its mention check."""
    clocked(sc, ctx, github)
    bug(github, ctx, SEED, 1)
    reset = ctx.clock.now().timestamp() + 600
    github.answers["GET /search/issues"] = [limit_answer(reset)]
    made = mention_checked_before_create(sc, github, monkeypatch)
    s = scheduler(sc, ctx)
    assert s.discover() == []
    assert s.cycle_cut and s.cycle_progress is not None and s.cycle_progress.seed_finished
    assert near(s.discover_after, datetime.fromtimestamp(int(reset), UTC))
    reads, searches = len(issue_reads(github, SEED)), len(searched(github, "repositories"))
    assert reads == 1
    advance(ctx, timedelta(seconds=601))
    s.tick()
    assert upstreams(sc, s.cycle_created) == [SEED] and made == [(SEED, 1)]
    assert len(issue_reads(github, SEED)) == reads and len(searched(github, "repositories")) == searches
    assert not s.cycle_cut and s.cycle_progress is None and s.last_discovery is not None
    assert github.writes == []


def test_a_stop_in_the_seed_loop_resumes_at_the_first_unread_target(sc: StageContext, ctx: AppContext,
                                                                    github: FakeGitHub) -> None:  # fmt: skip
    github.add_repo(SECOND, default_branch="main", language="Python")
    bug(github, ctx, SEED, 1)
    bug(github, ctx, SECOND, 2)
    sc.stopping = lambda: bool(issue_reads(github, SECOND))  # stop right after the second target's read
    s = scheduler(sc, ctx)
    assert s.discover() == []
    assert s.cycle_cut and near(s.discover_after, ctx.clock.now() + DISCOVER_RESUME_MIN)
    progress = s.cycle_progress
    assert progress is not None and {SEED, SECOND} <= progress.done and not progress.seed_finished
    first_reads = len([r for r in github.requests if r.path.startswith("/repos/")])
    sc.stopping = lambda: False
    created = s.discover()
    assert sorted(upstreams(sc, created)) == [SEED, SECOND]
    assert len(issue_reads(github, SEED)) == 1 and len(issue_reads(github, SECOND)) == 1  # not read again
    assert len([r for r in github.requests if r.path == f"/repos/{SEED}"]) == 1
    assert len([r for r in github.requests if r.path.startswith("/repos/")]) > first_reads  # the rest was read


def test_progress_older_than_the_interval_is_dropped(sc: StageContext, ctx: AppContext, github: FakeGitHub) -> None:
    bug(github, ctx, SEED, 1)
    sc.stopping = lambda: bool(issue_reads(github, SEED))
    s = scheduler(sc, ctx)
    assert s.discover() == [] and s.cycle_cut
    s.refused.add("o/r#9")
    sc.stopping = lambda: False
    advance(ctx, timedelta(hours=sc.config().policy.discovery.interval_hours, minutes=1))
    assert upstreams(sc, s.discover()) == [SEED]
    assert len(issue_reads(github, SEED)) == 2  # a full re-scan
    assert "o/r#9" not in s.refused  # refusals belong to one cycle


def test_a_fresh_full_cycle_waits_30_min_after_the_last_start(sc: StageContext, ctx: AppContext,
                                                              github: FakeGitHub) -> None:  # fmt: skip
    s = scheduler(sc, ctx)
    start = ctx.clock.now()
    assert s.discover() == [] and s.cycle_progress is None  # finished
    reads = len(github.requests)
    advance(ctx, timedelta(minutes=10))
    assert s.discover() == [] and len(github.requests) == reads  # refused: no read at all
    assert near(s.discover_after, start + FULL_RESCAN_MIN_GAP) and s.last_discovery is None
    advance(ctx, timedelta(minutes=20))
    s.tick()
    assert len(github.requests) > reads and s.last_discovery is not None


def test_a_gatekeeper_503_is_retried_after_5_min_with_the_progress(
    sc: StageContext, ctx: AppContext, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    bug(github, ctx, SEED, 1)
    real = sc.gk.create
    calls: list[int] = []

    def create(*args: Any) -> str:
        calls.append(1)
        if len(calls) == 1:
            raise GkError(503, "gatekeeper_unreachable")
        return real(*args)

    monkeypatch.setattr(sc.gk, "create", create)
    s = scheduler(sc, ctx)
    assert s.discover() == []
    assert s.cycle_cut and s.cycle_progress is not None and near(s.discover_after, ctx.clock.now() + DISCOVER_GK_RETRY)
    advance(ctx, timedelta(minutes=4))
    s.tick()
    assert len(calls) == 1  # not before the retry time
    advance(ctx, timedelta(minutes=1, seconds=1))
    s.tick()
    assert upstreams(sc, s.cycle_created) == [SEED] and len(issue_reads(github, SEED)) == 1
