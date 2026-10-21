"""The open phase inside ``discover.run`` (post-v1 open discovery, design §5.4, §6, §7; review-3 M2,
M3, L3; owner addition 2026-10-21: the default-branch activity gate), against the real gatekeeper
(in-process) and FakeGitHub. GitHub sees GET requests only."""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timedelta
from typing import Any

import pytest

from ossbot.common.config import KEPT_PRIORITY, OPEN_CATEGORY, ConfigBundle
from ossbot.common.schemas import PrefsUpdate, State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.runner import open_discovery as od
from ossbot.runner import scoring
from ossbot.runner.errors import Interrupted
from ossbot.runner.github_read import GitHubRateLimited, GitHubRead
from ossbot.runner.gk_client import GkError
from ossbot.runner.pipeline import StageContext
from ossbot.runner.stages import discover
from tests.conftest import insert_contribution
from tests.fakes.fake_github import FakeGitHub
from tests.unit.gatekeeper.test_discovered_targets import HERMES, OPERATOR, WORKER, facts

OPEN = "acme/gitops-tool"  # not in config/targets.yaml
SEED = "langfuse/langfuse"


def now_of(ctx: AppContext) -> datetime:
    return ctx.clock.now()


def iso(ctx: AppContext, days_ago: float) -> str:
    return (now_of(ctx) - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def open_repo(github: FakeGitHub, ctx: AppContext, name: str = OPEN, *, topic: str = "devops", stars: int = 5000,
              contributing: bool = True, **over: Any) -> None:  # fmt: skip
    doc: dict[str, Any] = {"stargazers_count": stars, "pushed_at": iso(ctx, 2), "language": "Go",
                           "license": {"spdx_id": "Apache-2.0"}, "topics": [topic], "has_issues": True,
                           "visibility": "public", "default_branch": "main"}  # fmt: skip
    github.add_repo(name, **{**doc, **over})
    if contributing:
        github.files[(name.lower(), "CONTRIBUTING.md")] = "Be kind; run the tests."


def issue(github: FakeGitHub, ctx: AppContext, repo: str, number: int, label: str = "good first issue") -> None:
    github.add_issue(repo, number, title=f"Fix typo in docs {number}", body="steps to reproduce",
                     labels=[{"name": label}], updated_at=iso(ctx, 1), created_at=iso(ctx, 10), comments=0)  # fmt: skip


def seed_issues(github: FakeGitHub, ctx: AppContext, n: int) -> None:
    for number in range(1, n + 1):
        github.add_issue(SEED, number, title=f"Crash {number}", body="steps to reproduce", labels=[{"name": "bug"}],
                         updated_at=iso(ctx, 1), created_at=iso(ctx, 20 + number), comments=0)  # fmt: skip


def upstreams(sc: StageContext, cids: list[str]) -> list[str]:
    return [str(sc.gk.contribution(cid)["upstream"]) for cid in cids]


def searched(github: FakeGitHub, kind: str) -> list[str]:
    return [r.params.get("q", "") for r in github.requests if r.path == f"/search/{kind}"]


def issue_searches(github: FakeGitHub, repo: str) -> list[str]:
    return [q for q in searched(github, "issues") if q.startswith(f"repo:{repo} is:issue")]


def row(ctx: AppContext, repo: str) -> dict[str, Any] | None:
    v = ctx.discovered.get(repo)
    return v.model_dump() if v is not None else None


def with_open(sc: StageContext, **over: Any) -> None:
    """Change ``policy.discovery.open`` for the runner's snapshot only."""
    bundle: ConfigBundle = sc.config()
    disc = bundle.policy.discovery
    policy = bundle.policy.model_copy(update={"discovery": disc.model_copy(update={
        "open": disc.open.model_copy(update=over)})})  # fmt: skip
    changed = dataclasses.replace(bundle, policy=policy)
    sc.config = lambda: changed


def watching(ctx: AppContext, repo: str) -> None:
    insert_contribution(ctx, State.WATCHING, upstream=repo, issue_number=900, pr_number=9, category=OPEN_CATEGORY)


# ------------------------------------------------------------------ the happy path, L3
def test_an_open_repo_becomes_one_contribution(sc: StageContext, ctx: AppContext, github: FakeGitHub,
                                               monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    open_repo(github, ctx)
    issue(github, ctx, OPEN, 41)
    github.add_pull(OPEN, state="closed", merged_at=iso(ctx, 5), head={"sha": "c1", "label": "x:c1"})
    github.check_runs[(OPEN.lower(), "c1")] = [{"name": "pytest", "status": "completed", "conclusion": "success"}]
    open_repo(github, ctx, "acme/forked", fork=True)
    open_repo(github, ctx, "acme/busl", license={"spdx_id": "BUSL-1.1"})
    seen: list[scoring.Ranked] = []
    real = discover._rank_open

    def spy(*args: Any, **kw: Any) -> list[scoring.Ranked]:
        out = real(*args, **kw)
        seen.extend(out)
        return out

    monkeypatch.setattr(discover, "_rank_open", spy)
    created = discover.run(sc, now=now_of(ctx))
    assert upstreams(sc, created) == [OPEN]
    c = sc.gk.contribution(created[0])
    assert (c["issue_number"], c["source"], c["category"]) == (41, "discover", OPEN_CATEGORY)
    r = row(ctx, OPEN)
    assert r is not None and (r["status"], r["source"], r["topic"], r["ci_on_pr"]) == (
        "candidate", "search", "devops", "yes")  # fmt: skip
    assert r["tried_at"] is not None and r["cooldown_until"] is not None
    assert row(ctx, "acme/forked") is None and row(ctx, "acme/busl") is None  # rules 3-12 write nothing
    hit = od.RepoHit.from_item(github.repos[OPEN.lower()])
    assert hit is not None
    base = od.repo_score_base(hit, min_stars=1000, now=now_of(ctx), languages_preferred=["Go"], language_weight=6.0)
    assert r["repo_score"] == pytest.approx(base + 6.0, abs=1e-3)  # CI counted in the repo score ...
    ranked = next(x for x in seen if x.candidate.number == 41)
    assert ranked.reasons["repo"] == pytest.approx(0.5 * r["repo_score"], abs=1e-3)
    assert "ci_on_pr" not in ranked.reasons  # ... and only there (L3)
    assert searched(github, "repositories")[0] == (
        f"topic:devops stars:>=1000 pushed:>={(now_of(ctx) - timedelta(days=60)).date().isoformat()} "
        "archived:false mirror:false is:public")  # fmt: skip
    assert issue_searches(github, OPEN) == [
        f'repo:{OPEN} is:issue is:open no:assignee archived:false '
        'label:"good first issue","help wanted",bug,documentation,docs']  # fmt: skip
    assert github.writes == []


def test_rule_13_failure_is_recorded_skipped_with_a_cooldown(sc: StageContext, ctx: AppContext,
                                                             github: FakeGitHub) -> None:  # fmt: skip
    open_repo(github, ctx, contributing=False)
    issue(github, ctx, OPEN, 41)
    assert discover.run(sc, now=now_of(ctx)) == []
    r = row(ctx, OPEN)
    assert r is not None and (r["status"], r["skip_reason"]) == ("skipped", "no_contributing")
    assert r["cooldown_until"] is not None and issue_searches(github, OPEN) == []


# ------------------------------------------------------------------ the default-branch activity gate
def test_a_stale_default_branch_skips_seed_and_records_open(sc: StageContext, ctx: AppContext, github: FakeGitHub,
                                                            caplog: pytest.LogCaptureFixture) -> None:  # fmt: skip
    """Owner addition: a seed target is skipped for the cycle (INFO, kept in targets.yaml); an open repo
    whose other branches are recent (pushed_at 2 days) but whose default branch is 7 months old is
    recorded skipped ``inactive_default_branch``."""
    seed_issues(github, ctx, 1)
    github.commit_dates[SEED] = iso(ctx, 213)
    open_repo(github, ctx)
    issue(github, ctx, OPEN, 41)
    github.commit_dates[OPEN.lower()] = iso(ctx, 213)
    with caplog.at_level(logging.INFO):
        assert discover.run(sc, now=now_of(ctx)) == []
    assert [r for r in caplog.records if getattr(r, "event", "") == "discover_repo_inactive"]
    assert not any(r.path == f"/repos/{SEED}/issues" for r in github.requests)
    r = row(ctx, OPEN)
    assert r is not None and (r["status"], r["skip_reason"]) == ("skipped", "inactive_default_branch")
    github.commit_dates[SEED] = iso(ctx, 3)  # the next cycle re-checks the seed target
    assert upstreams(sc, discover.run(sc, now=now_of(ctx))) == [SEED]


def test_a_failed_commit_read_is_unknown_not_a_skip(sc: StageContext, ctx: AppContext, github: FakeGitHub) -> None:
    seed_issues(github, ctx, 1)
    github.failures[f"GET /repos/{SEED}/commits"] = [403]
    assert upstreams(sc, discover.run(sc, repos=[SEED], now=now_of(ctx))) == [SEED]


@pytest.mark.parametrize(("days", "bonus"), [(2, 2.0), (20, 1.0), (90, 0.0), (None, 0.0)])
def test_a_recent_default_branch_is_a_small_boost(sc: StageContext, ctx: AppContext, github: FakeGitHub,
                                                  monkeypatch: pytest.MonkeyPatch, days: int | None,
                                                  bonus: float) -> None:  # fmt: skip
    seed_issues(github, ctx, 1)
    if days is not None:
        github.commit_dates[SEED] = iso(ctx, days)
    made: list[scoring.Candidate] = []
    real = scoring.candidate_from_issue

    def spy(*args: Any, **kw: Any) -> scoring.Candidate:
        made.append(real(*args, **kw))
        return made[-1]

    monkeypatch.setattr(scoring, "candidate_from_issue", spy)
    assert len(discover.run(sc, repos=[SEED], now=now_of(ctx))) == 1
    assert [c.repo_bonus for c in made] == [bonus]


# ------------------------------------------------------------------ the tiered merge (§7.3)
def ranked(repo: str, n: int, s: float) -> scoring.Ranked:
    now = datetime(2026, 10, 17)
    return scoring.Ranked(scoring.Candidate(repo, n, "t", "", (), now, now), s, "s", "bug")


def test_merge_order_with_reserved_slots() -> None:
    t0 = [ranked("s/r", i, 50 - i) for i in range(1, 5)]
    t1 = [ranked("o/r", 9, 10)]
    key = [(r.candidate.number, tier) for r, tier in discover._merge(t0, t1, 3, 1)]
    assert key == [(1, 0), (2, 0), (9, 1), (3, 0), (4, 0)]
    assert [(r.candidate.number, t) for r, t in discover._merge(t0, t1, 3, 0)][:4] == [(1, 0), (2, 0), (3, 0), (9, 1)]
    assert [t for _, t in discover._merge(t0, [], 3, 1)] == [0, 0, 0, 0]
    assert next((r.candidate.number, t) for r, t in discover._merge(t0, t1, 0, 1)) == (9, 1)


@pytest.mark.parametrize(("reserved", "expect"), [(1, [SEED, SEED, OPEN]), (0, [SEED, SEED, SEED])])
def test_seed_first_with_a_reserved_slot(sc: StageContext, ctx: AppContext, github: FakeGitHub, reserved: int,
                                         expect: list[str]) -> None:  # fmt: skip
    seed_issues(github, ctx, 4)
    open_repo(github, ctx)
    issue(github, ctx, OPEN, 41)
    with_open(sc, reserved_slots=reserved)
    assert upstreams(sc, discover.run(sc, now=now_of(ctx))) == expect


def test_an_unpaid_tier1_mention_check_gives_the_slot_back(sc: StageContext, ctx: AppContext, github: FakeGitHub,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    seed_issues(github, ctx, 4)
    spent = od.SearchBudget(0)
    cand = scoring.candidate_from_issue(OPEN, {"number": 41, "title": "Fix typo", "updated_at": iso(ctx, 1),
                                               "created_at": iso(ctx, 2)}, priority=5, category=OPEN_CATEGORY,
                                        language="Go")  # fmt: skip
    result = od.OpenResult(spent, [cand], {}, {})
    monkeypatch.setattr(discover, "_open_phase", lambda sc, **kw: result)
    refused: set[str] = set()
    assert upstreams(sc, discover.run(sc, now=now_of(ctx), refused=refused)) == [SEED, SEED, SEED]
    assert f"{OPEN}#41" not in refused  # skipped for this cycle, not cached as refused
    assert not any(q.startswith(f"repo:{OPEN} is:pr") for q in searched(github, "issues"))


def test_a_linked_pr_refuses_the_open_pick(sc: StageContext, ctx: AppContext, github: FakeGitHub) -> None:
    open_repo(github, ctx)
    issue(github, ctx, OPEN, 41)
    issue(github, ctx, OPEN, 42)
    github.mentions[(OPEN.lower(), 41)] = 1
    refused: set[str] = set()
    created = discover.run(sc, now=now_of(ctx), refused=refused)
    assert [sc.gk.contribution(c)["issue_number"] for c in created] == [42] and f"{OPEN}#41" in refused


# ------------------------------------------------------------------ when the open phase runs
def test_open_disabled_by_prefs_and_ossctl_discover_skip_the_search(sc: StageContext, ctx: AppContext,
                                                                    github: FakeGitHub) -> None:  # fmt: skip
    open_repo(github, ctx)
    issue(github, ctx, OPEN, 41)
    assert discover.run(sc, repos=[SEED], now=now_of(ctx)) == []
    assert searched(github, "repositories") == []
    ctx.discovery_prefs.update(PrefsUpdate(filters={"open_enabled": False}), "tg:4242")
    assert discover.run(sc, now=now_of(ctx)) == [] and searched(github, "repositories") == []


def test_a_search_limit_saves_search_until_and_defers(sc: StageContext, ctx: AppContext, github: FakeGitHub) -> None:
    """Finding 1: the open phase saves ``_open.search_until`` and re-raises; nothing is created without
    its mention check, earlier cids stay in ``created``; past the reset the cycle completes."""
    wall = [now_of(ctx).timestamp()]
    sc.gh = GitHubRead(github.sync_client(), sleep=lambda s: None, wall=lambda: wall[0], monotonic=lambda: wall[0])
    seed_issues(github, ctx, 2)
    open_repo(github, ctx)
    issue(github, ctx, OPEN, 41)
    reset = int(wall[0]) + 600
    limited = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}
    github.answers["GET /search/repositories"] = [(403, limited, {"message": "API rate limit exceeded"})]
    earlier = ["c_EARLIER001"]
    with pytest.raises(GitHubRateLimited):
        discover.run(sc, now=now_of(ctx), created=earlier)
    assert earlier == ["c_EARLIER001"] and sc.load(od.OPEN_NOTE)["search_until"] == reset
    assert db.one(ctx.conn, "SELECT COUNT(*) AS n FROM contributions")["n"] == 0  # type: ignore[index]
    ctx.clock.advance(seconds=601)  # type: ignore[attr-defined]
    wall[0] += 601
    created = discover.run(sc, now=now_of(ctx), created=earlier)
    assert sorted(upstreams(sc, created)) == [OPEN, SEED] and len(earlier) == 3


def test_the_cursor_is_saved_after_the_repo_searches(sc: StageContext, ctx: AppContext, github: FakeGitHub) -> None:
    """Finding 14: a search limit during the issue searches still moves the cursor, so the retried cycle
    searches the next topics."""
    open_repo(github, ctx)
    issue(github, ctx, OPEN, 41)
    github.answers["GET /search/issues"] = [(429, {"retry-after": "30"}, {"message": "slow down"})]
    with pytest.raises(GitHubRateLimited):
        discover.run(sc, now=now_of(ctx))
    assert sc.load(od.OPEN_NOTE)["cursor"] == 3
    topics = [q.split()[0] for q in searched(github, "repositories")]
    assert topics == ["topic:devops", "topic:platform-engineering", "topic:kubernetes"]
    sc.save(od.OPEN_NOTE, search_until=0)
    sc.gh.search_limited_until = 0.0
    discover.run(sc, now=now_of(ctx))
    assert [q.split()[0] for q in searched(github, "repositories")][3:] == [
        "topic:terraform", "topic:infrastructure-as-code", "topic:gitops"]  # fmt: skip


def test_github_unavailable_in_the_open_phase_ends_the_cycle(sc: StageContext, ctx: AppContext,
                                                             github: FakeGitHub) -> None:  # fmt: skip
    open_repo(github, ctx)
    github.failures[f"GET /repos/{OPEN}/community/profile"] = [502] * 4
    with pytest.raises(discover.GitHubUnavailable):
        discover.run(sc, now=now_of(ctx))


def test_a_4xx_search_ends_the_open_phase_and_the_merge_goes_on(sc: StageContext, ctx: AppContext,
                                                                github: FakeGitHub,
                                                                caplog: pytest.LogCaptureFixture) -> None:  # fmt: skip
    seed_issues(github, ctx, 1)
    github.failures["GET /search/repositories"] = [422]
    with caplog.at_level(logging.WARNING):
        assert upstreams(sc, discover.run(sc, now=now_of(ctx))) == [SEED]
    assert [r for r in caplog.records if getattr(r, "event", "") == "open_search_failed"]
    assert len(searched(github, "repositories")) == 1


def test_a_stop_between_searches_ends_the_cycle(sc: StageContext, ctx: AppContext, github: FakeGitHub) -> None:
    sc.stopping = lambda: bool(searched(github, "repositories"))
    with pytest.raises(Interrupted):
        discover.run(sc, now=now_of(ctx))
    assert len(searched(github, "repositories")) == 1


# ------------------------------------------------------------------ kept repos (§6.2)
def test_kept_repos_are_rechecked_then_scanned_without_a_search(sc: StageContext, ctx: AppContext,
                                                                github: FakeGitHub) -> None:  # fmt: skip
    kept = "acme/kept-tool"
    open_repo(github, ctx, kept, topic="unrelated", stars=10)  # stars and topic don't matter once kept
    issue(github, ctx, kept, 7, label="bug")
    ctx.discovered.decide(kept, "keep", OPERATOR)
    assert row(ctx, kept)["checked_at"] is None  # type: ignore[index]
    created = discover.run(sc, now=now_of(ctx))
    c = sc.gk.contribution(created[0])
    assert (c["upstream"], c["category"]) == (kept, OPEN_CATEGORY)
    r = row(ctx, kept)
    assert r is not None and r["status"] == "kept" and r["checked_at"] is not None and r["skip_reason"] is None
    assert issue_searches(github, kept) == [] and any(x.path == f"/repos/{kept}/issues" for x in github.requests)
    t = ctx.discovered.effective_target(kept)
    assert t is not None and t.priority == KEPT_PRIORITY


def test_a_kept_repo_with_a_skip_reason_is_not_scanned(sc: StageContext, ctx: AppContext, github: FakeGitHub) -> None:
    kept = "acme/kept-tool"
    open_repo(github, ctx, kept, archived=True)
    issue(github, ctx, kept, 7, label="bug")
    ctx.discovered.decide(kept, "keep", OPERATOR)
    assert discover.run(sc, now=now_of(ctx)) == []
    r = row(ctx, kept)
    assert r is not None and (r["status"], r["skip_reason"]) == ("kept", "archived")
    assert not any(x.path == f"/repos/{kept}/issues" for x in github.requests)
    reads = len([x for x in github.requests if x.path == f"/repos/{kept}"])
    discover.run(sc, now=now_of(ctx))  # not due: no read until repo_cooldown_days passed
    assert len([x for x in github.requests if x.path == f"/repos/{kept}"]) == reads


# ------------------------------------------------------------------ Hermes suggestions (§11)
def test_hermes_suggestions(sc: StageContext, ctx: AppContext, github: FakeGitHub) -> None:
    open_repo(github, ctx, "acme/small", stars=800)
    open_repo(github, ctx, "acme/named", topic="unrelated")
    issue(github, ctx, "acme/named", 5, label="enhancement")  # a named issue needs no label
    ctx.discovered.suggest("acme/small", None, HERMES)
    ctx.discovered.suggest("acme/named", 5, HERMES)
    created = discover.run(sc, now=now_of(ctx))
    assert upstreams(sc, created) == ["acme/named"]
    c = sc.gk.contribution(created[0])
    assert (c["issue_number"], c["source"]) == (5, "hermes_request")
    small = row(ctx, "acme/small")
    assert small is not None and (small["status"], small["skip_reason"]) == ("skipped", "stars_below_min")
    named = row(ctx, "acme/named")
    assert named is not None and named["status"] == "candidate" and named["source"] == "hermes"
    assert issue_searches(github, "acme/named") == []


def test_at_most_three_suggestions_per_cycle(sc: StageContext, ctx: AppContext, github: FakeGitHub) -> None:
    for i in range(5):
        open_repo(github, ctx, f"acme/s{i}", topic="unrelated")
        ctx.discovered.suggest(f"acme/s{i}", None, HERMES)
        ctx.clock.advance(seconds=1)  # type: ignore[attr-defined]
    discover.run(sc, now=now_of(ctx))
    statuses = [row(ctx, f"acme/s{i}")["status"] for i in range(5)]  # type: ignore[index]
    assert statuses == ["candidate"] * 3 + ["suggested"] * 2


# ------------------------------------------------------------------ M2: one open PR per repo
def test_m2_an_open_pr_keeps_a_search_hit_out(sc: StageContext, ctx: AppContext, github: FakeGitHub) -> None:
    open_repo(github, ctx)
    issue(github, ctx, OPEN, 41)
    watching(ctx, OPEN)
    assert discover.run(sc, now=now_of(ctx)) == []
    assert row(ctx, OPEN) is None and issue_searches(github, OPEN) == []


def test_m2_an_open_pr_keeps_a_carry_over_row_unchanged(sc: StageContext, ctx: AppContext,
                                                        github: FakeGitHub) -> None:  # fmt: skip
    open_repo(github, ctx, topic="unrelated")
    issue(github, ctx, OPEN, 41)
    ctx.discovered.record(facts(OPEN), WORKER)  # an untried candidate: the next cycle's carry-over
    watching(ctx, OPEN)
    before = row(ctx, OPEN)
    assert discover.run(sc, now=now_of(ctx)) == []
    assert row(ctx, OPEN) == before and issue_searches(github, OPEN) == []


def test_m2_an_open_pr_keeps_a_suggestion_and_its_named_issue_unchanged(sc: StageContext, ctx: AppContext,
                                                                        github: FakeGitHub) -> None:  # fmt: skip
    for repo, number in (("acme/plain", None), ("acme/named", 5)):
        open_repo(github, ctx, repo, topic="unrelated")
        issue(github, ctx, repo, 5)
        ctx.discovered.suggest(repo, number, HERMES)
        watching(ctx, repo)
    before = {r: row(ctx, r) for r in ("acme/plain", "acme/named")}
    assert discover.run(sc, now=now_of(ctx)) == []
    assert {r: row(ctx, r) for r in before} == before
    assert not any(x.path in {"/repos/acme/named/issues/5", "/repos/acme/plain"} for x in github.requests)


def test_carry_over_is_scanned_without_a_repo_search(sc: StageContext, ctx: AppContext, github: FakeGitHub) -> None:
    open_repo(github, ctx, topic="unrelated")
    issue(github, ctx, OPEN, 41)
    ctx.discovered.record(facts(OPEN), WORKER)
    assert upstreams(sc, discover.run(sc, now=now_of(ctx))) == [OPEN]
    r = row(ctx, OPEN)
    assert r is not None and r["tried_at"] is not None and not any(x.path == f"/repos/{OPEN}" for x in github.requests)


# ------------------------------------------------------------------ M3: answers keyed on the code
@pytest.mark.parametrize(("code", "level", "event"), [("schema_error", logging.ERROR, "open_facts_invalid"),
                                                       ("denied", logging.INFO, "open_repo_refused")])  # fmt: skip
def test_m3_gatekeeper_answers_are_keyed_on_the_code(sc: StageContext, ctx: AppContext, github: FakeGitHub,
                                                     monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
                                                     code: str, level: int, event: str) -> None:  # fmt: skip
    open_repo(github, ctx)
    issue(github, ctx, OPEN, 41)

    def refuse(body: dict[str, Any]) -> dict[str, Any]:
        raise GkError(400, code)

    monkeypatch.setattr(sc.gk, "record_discovered", refuse)
    with caplog.at_level(logging.INFO, logger="ossbot.runner.open_discovery"):
        assert discover.run(sc, now=now_of(ctx)) == []
    hits = [r for r in caplog.records if getattr(r, "event", "") == event]
    assert hits and hits[0].levelno == level and issue_searches(github, OPEN) == []


def test_m3_a_gatekeeper_5xx_is_reraised(sc: StageContext, ctx: AppContext, github: FakeGitHub,
                                          monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    open_repo(github, ctx)

    def down(body: dict[str, Any]) -> dict[str, Any]:
        raise GkError(503, "gatekeeper_unreachable")

    monkeypatch.setattr(sc.gk, "record_discovered", down)
    with pytest.raises(GkError):
        discover.run(sc, now=now_of(ctx))


def test_an_unknown_egress_mode_runs_rule_14(sc: StageContext, ctx: AppContext, github: FakeGitHub,
                                             monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    open_repo(github, ctx)
    issue(github, ctx, OPEN, 41)
    github.files[(OPEN.lower(), "Chart.yaml")] = "dependencies:\n  - repository: https://charts.example.io\n"

    def broken() -> str:
        raise GkError(503, "gatekeeper_unreachable")

    monkeypatch.setattr(sc.gk, "egress_mode", broken)
    assert discover.run(sc, now=now_of(ctx)) == []
    r = row(ctx, OPEN)
    assert r is not None and r["skip_reason"] == "egress_extra_needed:charts.example.io"
