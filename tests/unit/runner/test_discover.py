"""The discover stage's seed loop (discovery pacing, ADR-0003 addendum 2026-10-22, fix 3): one core
``GET /repos/{r}/issues`` per seed or kept repo with the labels OR-matched on the client, and the core
floor that stops the reads and still finishes the cycle. GitHub sees GET requests only."""

from __future__ import annotations

import dataclasses
import logging
from datetime import timedelta

import pytest
from pydantic import ValidationError

from ossbot.common.config import all_targets
from ossbot.common.schemas import DiscoveryCycleReport, State
from ossbot.gatekeeper.context import AppContext
from ossbot.runner.pipeline import StageContext
from ossbot.runner.stages import discover
from tests.conftest import insert_contribution
from tests.fakes.fake_github import FakeGitHub
from tests.unit.gatekeeper.test_discovered_targets import OPERATOR
from tests.unit.runner.test_discover_open import (
    issue_reads,
    mention_checked_before_create,
    now_of,
    open_repo,
    searched,
)

SEED = "langfuse/langfuse"  # the first enabled target of config/targets.yaml


def iso(ctx: AppContext, days_ago: float) -> str:
    return (now_of(ctx) - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def seed_issue(github: FakeGitHub, ctx: AppContext, number: int, *labels: str, repo: str = SEED) -> None:
    github.add_issue(repo, number, title=f"Crash {number}", body="steps to reproduce",
                     labels=[{"name": x} for x in labels], updated_at=iso(ctx, 1), created_at=iso(ctx, 20),
                     comments=0)  # fmt: skip


def numbers(sc: StageContext, cids: list[str]) -> list[int]:
    return sorted(int(sc.gk.contribution(c)["issue_number"]) for c in cids)


def test_a_seed_target_costs_one_issues_read_with_the_labels_ored(sc: StageContext, ctx: AppContext,
                                                                  github: FakeGitHub) -> None:  # fmt: skip
    """The six default labels used to cost six ``labels=`` calls (an AND each); now one page of 100."""
    target = next(t for t in all_targets(ctx.config.targets) if t.repo == SEED)
    assert len(target.labels) == 6
    seed_issue(github, ctx, 1, "kind/bug")  # only the last-but-one configured label
    seed_issue(github, ctx, 2, "Good First Issue")  # case-insensitive
    seed_issue(github, ctx, 3, "enhancement")  # no configured label
    seed_issue(github, ctx, 4)  # no label at all
    created = discover.run(sc, repos=[SEED], now=now_of(ctx))
    assert numbers(sc, created) == [1, 2]
    assert issue_reads(github, SEED) == [{"state": "open", "sort": "updated", "per_page": "100"}]
    assert github.writes == []


def test_a_target_without_labels_takes_every_open_issue(sc: StageContext, ctx: AppContext, github: FakeGitHub,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    target = next(t for t in all_targets(ctx.config.targets) if t.repo == SEED)
    bare = dataclasses.replace(target, labels=())
    real = discover.all_targets
    monkeypatch.setattr(discover, "all_targets",
                        lambda targets: [bare if t.repo == SEED else t for t in real(targets)])  # fmt: skip
    seed_issue(github, ctx, 5, "enhancement")
    seed_issue(github, ctx, 6)
    assert numbers(sc, discover.run(sc, repos=[SEED], now=now_of(ctx))) == [5, 6]
    assert len(issue_reads(github, SEED)) == 1


def test_a_kept_repo_costs_one_issues_read(sc: StageContext, ctx: AppContext, github: FakeGitHub) -> None:
    kept = "acme/kept-tool"
    open_repo(github, ctx, kept, topic="unrelated", stars=10)
    seed_issue(github, ctx, 7, "Bug", repo=kept)
    seed_issue(github, ctx, 8, "question", repo=kept)  # a skip label
    ctx.discovered.decide(kept, "keep", OPERATOR)
    created = discover.run(sc, now=now_of(ctx))
    assert [(sc.gk.contribution(c)["upstream"], sc.gk.contribution(c)["issue_number"]) for c in created] == [
        (kept, 7)]  # fmt: skip
    assert len(issue_reads(github, kept)) == 1


def test_the_core_floor_stops_reading_and_the_cycle_still_finishes(
    sc: StageContext,
    ctx: AppContext,
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Below ``CORE_FLOOR`` core requests left (GitHub's header after the first target), the seed and kept
    loops stop, the open phase and CI reads are skipped, and the cycle creates from what it read, each
    pick after its mention check."""
    github.core_headers = {"x-ratelimit-remaining": "900"}
    seed_issue(github, ctx, 1, "bug")
    seed_issue(github, ctx, 2, "bug")
    kept = "acme/kept-tool"
    open_repo(github, ctx, kept, topic="unrelated", stars=10)
    seed_issue(github, ctx, 7, "bug", repo=kept)
    ctx.discovered.decide(kept, "keep", OPERATOR)
    open_repo(github, ctx, "acme/gitops-tool", topic="devops")
    made = mention_checked_before_create(sc, github, monkeypatch)
    with caplog.at_level(logging.INFO, logger="ossbot.runner.stages.discover"):
        created = discover.run(sc, now=now_of(ctx))
    assert numbers(sc, created) == [1, 2] and len(made) == 2
    floor = [r for r in caplog.records if getattr(r, "event", "") == "discover_core_floor"]
    assert len(floor) == 1 and floor[0].remaining == 900  # type: ignore[attr-defined]
    core_paths = {r.path for r in github.requests if r.path.startswith("/repos/")}
    assert core_paths == {f"/repos/{SEED}", f"/repos/{SEED}/commits", f"/repos/{SEED}/issues"}  # no kept/open/CI read
    assert searched(github, "repositories") == []
    assert github.writes == []


SECOND = "langfuse/langfuse-python"  # another enabled target


def test_discover_done_counts_and_the_cycle_report(sc: StageContext, ctx: AppContext, github: FakeGitHub,
                                                   caplog: pytest.LogCaptureFixture) -> None:  # fmt: skip
    """Discovery fix 5a: two seed targets (one inactive), three candidates: one with a linked PR, one the
    gatekeeper refuses (an active item on the issue), one created. The report body validates."""
    github.add_repo(SECOND, default_branch="main", language="Python")
    github.commit_dates[SECOND.lower()] = iso(ctx, 400)  # no commit on main for > 180 days
    for n in (1, 2, 3):
        seed_issue(github, ctx, n, "bug")
    github.mentions[(SEED.lower(), 2)] = 1
    insert_contribution(ctx, State.VETTING, upstream=SEED, issue_number=3)
    progress = discover.CycleProgress(now_of(ctx))
    with caplog.at_level(logging.INFO, logger="ossbot.runner.stages.discover"):
        created = discover.run(sc, repos=[SEED, SECOND], now=now_of(ctx), progress=progress)
    assert numbers(sc, created) == [1]
    (done,) = [r for r in caplog.records if getattr(r, "event", "") == "discover_done"]
    counts = done.counts  # type: ignore[attr-defined]
    assert done.scope == "repos"  # type: ignore[attr-defined]
    assert {k: counts[k] for k in ("targets_read", "inactive", "candidates", "ranked", "linked_pr", "refused",
                                   "created_total", "open_candidates", "resumes")} == {
        "targets_read": 1, "inactive": 1, "candidates": 3, "ranked": 3, "linked_pr": 1, "refused": 1,
        "created_total": 1, "open_candidates": 0, "resumes": 0}  # fmt: skip
    body = discover.cycle_report(progress, outcome="finished", scope="repos", cut_reason=None, created=created,
                                 ended_at=now_of(ctx))  # fmt: skip
    report = DiscoveryCycleReport.model_validate(body)
    assert report.created == created and report.created_total == 1 and report.cut_reason is None
    assert github.writes == []


def test_the_report_carries_the_top_drops_and_at_most_ten_cids(ctx: AppContext) -> None:
    progress = discover.CycleProgress(now_of(ctx))
    progress.stats.drops.update({f"reason_{i:02d}": 20 - i for i in range(15)} | {"Bad Reason!": 99})
    cids = [f"c_{i:010d}" for i in range(12)]
    body = discover.cycle_report(progress, outcome="cut", scope="full", cut_reason="search_limit", created=cids,
                                 ended_at=now_of(ctx))  # fmt: skip
    report = DiscoveryCycleReport.model_validate(body)
    assert list(report.drops) == [f"reason_{i:02d}" for i in range(12)]  # the top 12, a bad code dropped
    assert len(report.created) == 10 and report.created_total == 12 and report.cut_reason == "search_limit"


@pytest.mark.parametrize(
    "change",
    [{"outcome": "cut"}, {"cut_reason": "stopped"}, {"bogus": 1}, {"candidates": -1}, {"candidates": 100_001},
     {"drops": {"Bad Reason": 1}}, {"drops": {f"r{i}": 1 for i in range(13)}}, {"created": ["c_bad"]},
     {"cut_reason": "anything"}, {"started_at": "yesterday"}],
    ids=["cut-without-reason", "reason-without-cut", "extra-key", "negative", "too-big", "bad-reason-code",
         "too-many-drops", "bad-cid", "unknown-cut-reason", "bad-timestamp"],
)  # fmt: skip
def test_the_report_schema_is_bounded(ctx: AppContext, change: dict[str, object]) -> None:
    body = discover.cycle_report(discover.CycleProgress(now_of(ctx)), outcome="finished", scope="full",
                                 cut_reason=None, created=[], ended_at=now_of(ctx))  # fmt: skip
    DiscoveryCycleReport.model_validate(body)
    with pytest.raises(ValidationError):
        DiscoveryCycleReport.model_validate({**body, **change})


def test_a_stale_core_remaining_does_not_stop_the_next_cycle(sc: StageContext, ctx: AppContext,
                                                             github: FakeGitHub) -> None:  # fmt: skip
    """A low ``x-ratelimit-remaining`` from before its reset is not the floor."""
    sc.gh.core_remaining, sc.gh.core_reset = 10, 1.0  # reset long past
    seed_issue(github, ctx, 1, "bug")
    assert numbers(sc, discover.run(sc, repos=[SEED], now=now_of(ctx))) == [1]
