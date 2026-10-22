"""The scheduler's discovery cycle across a cut (discovery pacing, ADR-0003 addendum 2026-10-22, fix 4):
a cut full cycle keeps its ``CycleProgress`` and resumes only the unfinished part; progress older than
``interval_hours`` is dropped; a fresh full cycle never starts within 30 min of the previous one; a
gatekeeper 503 is retried after 5 min. Real gatekeeper in-process, FakeGitHub, GET requests only."""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ossbot.common.config import ConfigError, PolicyConfig, parse_yaml_model
from ossbot.common.schemas import State
from ossbot.gatekeeper.context import AppContext
from ossbot.runner.errors import Interrupted
from ossbot.runner.github_read import GitHubRateLimited, GitHubRead
from ossbot.runner.gk_client import GkError
from ossbot.runner.pipeline import StageContext
from ossbot.runner.scheduler import (
    DISCOVER_GK_RETRY,
    DISCOVER_RESUME_MIN,
    FULL_RESCAN_MIN_GAP,
    Scheduler,
    cut_reason,
)
from ossbot.runner.stages import discover
from tests.conftest import insert_contribution
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


# ------------------------------------------------- discovery fix 5a: cycle reports, cut reasons
def reports(ctx: AppContext) -> list[dict[str, Any]]:
    rows = ctx.conn.execute("SELECT data_json FROM audit_events WHERE event = 'discovery_cycle' ORDER BY seq")
    return [json.loads(r[0]) for r in rows]


def test_a_finished_cycle_is_reported(sc: StageContext, ctx: AppContext, github: FakeGitHub) -> None:
    bug(github, ctx, SEED, 1)
    s = scheduler(sc, ctx)
    created = s.discover()
    (report,) = reports(ctx)
    assert report["outcome"] == "finished" and report["scope"] == "full" and report["cut_reason"] is None
    assert report["created"] == created and report["created_total"] == 1 and report["candidates"] == 1
    s.discover([SEED])  # ossctl discover: its own report
    assert [(r["outcome"], r["scope"]) for r in reports(ctx)] == [("finished", "full"), ("finished", "repos")]


def test_a_cut_is_reported_with_its_reason_and_the_resume_counts(
    sc: StageContext, ctx: AppContext, github: FakeGitHub, caplog: pytest.LogCaptureFixture
) -> None:
    clocked(sc, ctx, github)
    bug(github, ctx, SEED, 1)
    github.answers["GET /search/issues"] = [limit_answer(ctx.clock.now().timestamp() + 600)]
    s = scheduler(sc, ctx)
    with caplog.at_level(logging.INFO, logger="ossbot.runner.scheduler"):
        assert s.discover() == []
    (cut,) = [r for r in caplog.records if getattr(r, "event", "") == "discover_interrupted"]
    assert cut.cut_reason == "search_limit"  # type: ignore[attr-defined]
    (report,) = reports(ctx)
    assert (report["outcome"], report["cut_reason"], report["created_total"]) == ("cut", "search_limit", 0)
    advance(ctx, timedelta(seconds=601))
    s.tick()
    last = reports(ctx)[-1]
    assert last["outcome"] == "finished" and last["resumes"] == 1 and last["created_total"] == 1
    assert last["targets_read"] == report["targets_read"] > 0  # every target read once, before the cut


def test_cut_reasons_by_exception() -> None:
    assert cut_reason(GitHubRateLimited(0, "x", "search")) == "search_limit"
    assert cut_reason(GitHubRateLimited(0, "x")) == "core_limit"
    assert cut_reason(discover.GitHubUnavailable("x")) == "github_unavailable"
    assert cut_reason(Interrupted("stop")) == "stopped"


def test_a_failing_report_changes_nothing(sc: StageContext, ctx: AppContext, github: FakeGitHub,
                                          monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    bug(github, ctx, SEED, 1)
    sc.stopping = lambda: bool(issue_reads(github, SEED))

    def refuse(report: dict[str, Any]) -> None:
        raise GkError(500, "internal_error")

    monkeypatch.setattr(sc.gk, "discovery_cycle", refuse)
    s = scheduler(sc, ctx)
    assert s.discover() == []
    assert s.cycle_cut and near(s.discover_after, ctx.clock.now() + DISCOVER_RESUME_MIN)
    sc.stopping = lambda: False
    advance(ctx, DISCOVER_RESUME_MIN)
    assert upstreams(sc, s.discover()) == [SEED] and s.cycle_progress is None
    assert reports(ctx) == []


# ------------------------------------------------- discovery fix 7: target_proposals_per_day
def with_target(sc: StageContext, ctx: AppContext, target: int) -> None:
    bundle = ctx.config
    disc = bundle.policy.discovery.model_copy(update={"target_proposals_per_day": target})
    changed = dataclasses.replace(bundle, policy=bundle.policy.model_copy(update={"discovery": disc}))
    sc.config = lambda: changed


def cycles(s: Scheduler, monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    seen: list[datetime] = []
    real = s.discover

    def spy(repos: list[str] | None = None) -> list[str]:
        seen.append(s.now())
        return real(repos)

    monkeypatch.setattr(s, "discover", spy)
    return seen


def half(sc: StageContext) -> timedelta:
    return timedelta(hours=sc.config().policy.discovery.interval_hours) / 2


def test_below_target_the_next_cycle_runs_after_half_the_interval(
    sc: StageContext, ctx: AppContext, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert sc.config().policy.discovery.target_proposals_per_day == 2  # the shipped default
    s = scheduler(sc, ctx)
    seen = cycles(s, monkeypatch)
    s.tick()
    assert len(seen) == 1
    advance(ctx, half(sc) - timedelta(minutes=1))
    s.tick()
    assert len(seen) == 1  # not before interval / 2
    advance(ctx, timedelta(minutes=1))
    with caplog.at_level(logging.INFO, logger="ossbot.runner.scheduler"):
        s.tick()
    assert len(seen) == 2 and any(getattr(r, "event", "") == "discover_early" for r in caplog.records)


@pytest.mark.parametrize("case", ["target_reached", "daily_cap_used", "item_in_flight", "target_off", "gk_error"])
def test_otherwise_the_full_interval_applies(sc: StageContext, ctx: AppContext, monkeypatch: pytest.MonkeyPatch,
                                             case: str) -> None:  # fmt: skip
    if case == "target_reached":
        monkeypatch.setattr(sc.gk, "pacing", lambda: {"max_per_day": 5, "used": 2, "remaining": 3})
    elif case == "daily_cap_used":
        monkeypatch.setattr(sc.gk, "pacing", lambda: {"max_per_day": 1, "used": 1, "remaining": 0})
        with_target(sc, ctx, 2)
    elif case == "item_in_flight":
        insert_contribution(ctx, State.VETTING, upstream="langfuse/langfuse", issue_number=77)
    elif case == "target_off":
        with_target(sc, ctx, 0)
    else:

        def down() -> dict[str, Any]:
            raise GkError(503, "gatekeeper_unreachable")

        monkeypatch.setattr(sc.gk, "pacing", down)
    s = scheduler(sc, ctx)
    seen = cycles(s, monkeypatch)
    s.tick()
    advance(ctx, half(sc) + timedelta(minutes=5))
    s.tick()
    assert len(seen) == 1
    advance(ctx, half(sc))
    s.tick()
    assert len(seen) == 2  # the full interval


def test_a_user_item_in_flight_does_not_hold_the_early_cycle(sc: StageContext, ctx: AppContext,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    insert_contribution(ctx, State.VETTING, upstream="langfuse/langfuse", issue_number=78, source="user")
    s = scheduler(sc, ctx)
    seen = cycles(s, monkeypatch)
    s.tick()
    advance(ctx, half(sc))
    s.tick()
    assert len(seen) == 2


def test_the_target_never_exceeds_max_per_day() -> None:
    text = b"version: 1\nproposal: {max_per_day: 1}\n"
    with pytest.raises(ConfigError, match=r"target_proposals_per_day 2 exceeds proposal\.max_per_day 1"):
        parse_yaml_model(PolicyConfig, "policy.yaml", text)
