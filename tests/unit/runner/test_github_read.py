"""FEAT-006 review round 3 (F006-R3-1): the runner's GitHub reads follow DESIGN §20. A network
error or a 5xx is retried after 2, 8 and 30 s and then fails the stage as ``github_unavailable``;
a rate limit delays the stage (and every read) until the reset, at most 1 h, instead of failing it."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ossbot.common.config import all_targets
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.runner import github_read, verify_targets
from ossbot.runner.errors import Deferred, Interrupted, StageFailed
from ossbot.runner.github_read import GitHubRateLimited, GitHubRead, GitHubReadError
from ossbot.runner.pipeline import StageContext
from ossbot.runner.scheduler import DISCOVER_UNAVAILABLE_RETRY, FULL_RESCAN_MIN_GAP, Scheduler
from ossbot.runner.stages import discover
from tests.conftest import insert_contribution
from tests.fakes.fake_github import FakeGitHub

T0 = 1_790_000_000.0
Answer = Callable[[httpx.Request], httpx.Response]


class Wall:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


def reader(
    answers: list[Answer | Exception], wall: Callable[[], float] = lambda: T0
) -> tuple[GitHubRead, list[float], list[str]]:
    """A GitHubRead on a scripted transport (the last answer repeats): it, its sleeps, its requests."""
    slept: list[float] = []
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer(request)

    client = github_read.make_client("t", transport=httpx.MockTransport(handler))
    return GitHubRead(client, sleep=slept.append, wall=wall), slept, seen


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"full_name": "o/r"})


def status(code: int, **headers: str) -> Answer:
    return lambda request: httpx.Response(code, json={"message": "x"}, headers=headers)


REFUSED = httpx.ConnectError("[Errno 111] Connection refused")


@pytest.mark.parametrize(
    ("answers", "sleeps"),
    [
        ([REFUSED, ok], [2.0]),
        ([status(502), ok], [2.0]),
        ([REFUSED, status(503), REFUSED, ok], [2.0, 8.0, 30.0]),
    ],
    ids=["connect-refused", "502-then-200", "three-then-200"],
)
def test_network_errors_and_5xx_are_retried(answers: list[Answer | Exception], sleeps: list[float]) -> None:
    gh, slept, seen = reader(answers)
    assert gh.repo("o/r") == {"full_name": "o/r"}
    assert slept == sleeps and len(seen) == len(sleeps) + 1


@pytest.mark.parametrize("answer", [REFUSED, status(500)], ids=["refused", "500"])
def test_after_the_retries_the_stage_fails_as_github_unavailable(answer: Answer | Exception) -> None:
    gh, slept, seen = reader([answer])
    with pytest.raises(GitHubReadError) as info:
        gh.issue("o/r", 1)
    assert slept == list(github_read.RETRY_DELAYS_S) == [2.0, 8.0, 30.0] and len(seen) == 4
    assert isinstance(info.value, StageFailed) and info.value.error_class == "github_unavailable"


def test_a_plain_4xx_is_not_retried() -> None:
    gh, slept, seen = reader([status(403)])  # no limit headers: no access, not a rate limit
    with pytest.raises(GitHubReadError) as info:
        gh.repo("o/r")
    assert (info.value.error_class, info.value.status, slept, len(seen)) == ("github_http_403", 403, [], 1)
    gh404, _, _ = reader([status(404)])
    assert gh404.repo("o/r") is None


@pytest.mark.parametrize(
    ("answer", "wait"),
    [
        (status(403, **{"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(T0) + 120)}), 120.0),
        (status(403, **{"retry-after": "30"}), 30.0),  # secondary rate limit
        (status(429), github_read.RATE_LIMIT_DEFAULT_S),
        (status(429, **{"retry-after": "7200"}), github_read.RATE_LIMIT_MAX_S),  # at most 1 h
    ],
    ids=["primary", "secondary", "429-no-header", "capped"],
)
def test_a_rate_limit_delays_every_read_until_the_reset(answer: Answer, wait: float) -> None:
    wall = Wall()
    gh, slept, seen = reader([answer, ok], wall)
    with pytest.raises(GitHubRateLimited) as info:
        gh.repo("o/r")
    assert isinstance(info.value, Deferred) and not isinstance(info.value, StageFailed)
    assert info.value.until == T0 + wait and slept == []  # delayed, not slept in the stage thread
    with pytest.raises(GitHubRateLimited):
        gh.pulls("o/r")  # no request before the reset
    assert len(seen) == 1
    wall.t = T0 + wait
    assert gh.repo("o/r") == {"full_name": "o/r"} and len(seen) == 2


# ------------------------------------------------------------------ the scheduler side
def item(ctx: AppContext, cid: str) -> tuple[str, str | None]:
    row = db.one(ctx.conn, "SELECT state, state_reason FROM contributions WHERE id = ?", (cid,))
    assert row is not None
    return str(row["state"]), row["state_reason"]


def ready(sc: StageContext, ctx: AppContext) -> Scheduler:
    scheduler = Scheduler(sc, synchronous=True, now=ctx.clock.now)
    scheduler.first_start_done = True
    scheduler.last_discovery = scheduler.last_catalog = ctx.clock.now()
    return scheduler


def test_vet_fails_as_github_unavailable_not_internal(sc: StageContext, ctx: AppContext) -> None:
    """The live case: proxy 3140 refused while vet read the repo."""
    sc.gh, slept, _ = reader([REFUSED])
    cid = insert_contribution(ctx, State.DISCOVERED)
    assert cid in ready(sc, ctx).tick().started
    assert item(ctx, cid) == ("FAILED", "github_unavailable") and slept == [2.0, 8.0, 30.0]


def near(a: datetime, b: datetime) -> bool:
    return abs((a - b).total_seconds()) < 1


def test_a_rate_limited_stage_is_delayed_until_the_reset(sc: StageContext, ctx: AppContext) -> None:
    reset = int(ctx.clock.now().timestamp()) + 600
    limit = status(403, **{"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)})
    sc.gh, _, seen = reader([limit, ok], lambda: ctx.clock.now().timestamp())
    cid = insert_contribution(ctx, State.DISCOVERED)
    scheduler = ready(sc, ctx)
    assert cid in scheduler.tick().started
    assert item(ctx, cid)[0] == "VETTING"  # not FAILED
    state, until = scheduler.delayed[cid]
    assert state == "VETTING" and near(until, datetime.fromtimestamp(reset, UTC))
    ctx.clock.advance(seconds=60)  # type: ignore[attr-defined]
    assert cid not in scheduler.tick().started and len(seen) == 1  # waiting, no request
    ctx.clock.advance(seconds=600)  # type: ignore[attr-defined]
    assert cid in scheduler.tick().started and len(seen) > 1
    assert cid not in scheduler.delayed


def test_a_rate_limited_discovery_cycle_waits_for_the_reset(sc: StageContext, ctx: AppContext) -> None:
    reset = ctx.clock.now() + timedelta(seconds=900)
    sc.gh, _, seen = reader([status(429, **{"retry-after": "900"}), ok], lambda: ctx.clock.now().timestamp())
    scheduler = ready(sc, ctx)
    scheduler.last_discovery = None
    scheduler.tick()
    assert scheduler.last_discovery is None and len(seen) == 1
    assert scheduler.discover_after is not None and near(scheduler.discover_after, reset)
    ctx.clock.advance(seconds=300)  # type: ignore[attr-defined]
    scheduler.tick()
    assert len(seen) == 1  # no cycle before the reset
    ctx.clock.advance(seconds=timedelta(minutes=10).total_seconds())  # type: ignore[attr-defined]
    scheduler.tick()
    assert len(seen) > 1 and scheduler.last_discovery is not None and scheduler.discover_after is None


# ------------------------------------------- F006-R4-1: an outage costs one read's retries
def repos_read(seen: list[str]) -> set[str]:
    return {"/".join(p.split("/")[2:4]) for p in seen if p.startswith("/repos/")}


def test_github_unavailable_ends_the_discovery_cycle_and_dispatch_goes_on(
    sc: StageContext, ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusing proxy over every target (config/targets.yaml has dozens): the cycle stops after
    the first target's retries, so the tick dispatches the owner's Start work in the same tick."""
    assert sum(1 for t in all_targets(ctx.config.targets) if t.enabled and t.discovery) >= 3
    sc.gh, slept, seen = reader([REFUSED], lambda: ctx.clock.now().timestamp())
    approved = insert_contribution(ctx, State.PROPOSAL_APPROVED)
    scheduler = ready(sc, ctx)
    scheduler.last_discovery = None
    ran: list[str] = []
    monkeypatch.setattr(scheduler, "run_one", ran.append)
    assert approved in scheduler.tick().started and ran == [approved]
    assert sum(slept) <= 40 and len(repos_read(seen)) == 1  # one read's retries, not one per target
    assert scheduler.last_discovery is None  # the full cycle runs again ...
    assert scheduler.discover_after is not None
    assert near(scheduler.discover_after, ctx.clock.now() + DISCOVER_UNAVAILABLE_RETRY)
    reads = len(seen)
    ctx.clock.advance(seconds=300)  # type: ignore[attr-defined]
    scheduler.tick()
    assert len(seen) == reads  # ... but not before the retry time
    ctx.clock.advance(seconds=DISCOVER_UNAVAILABLE_RETRY.total_seconds())  # type: ignore[attr-defined]
    scheduler.tick()
    assert len(seen) > reads


def test_a_cut_cycle_keeps_its_spent_budget_for_the_rerun(
    sc: StageContext, ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F006-R5-1: a full cycle that created one item and then hit github_unavailable re-runs with that
    item (and its refusals) carried, so it creates at most budget - 1 more; the next cycle starts fresh."""
    seen: list[tuple[list[str], set[str]]] = []
    progresses: list[object] = []

    def fake_run(sc_: StageContext, *, repos: list[str] | None = None, now: datetime | None = None,
                 refused: set[str], created: list[str] | None = None,
                 progress: discover.CycleProgress | None = None) -> list[str]:  # fmt: skip
        assert created is not None and progress is not None
        seen.append((list(created), set(refused)))
        progresses.append(progress)
        if len(seen) == 1:
            created.append("c1")
            refused.add("o/r#2")
            raise discover.GitHubUnavailable("down")
        return []

    monkeypatch.setattr(discover, "run", fake_run)
    scheduler = ready(sc, ctx)
    assert scheduler.discover() == []  # cut
    assert scheduler.discover() == []  # the re-run of the same cycle
    assert seen[1] == (["c1"], {"o/r#2"}) and progresses[1] is progresses[0]  # its progress too
    ctx.clock.advance(seconds=FULL_RESCAN_MIN_GAP.total_seconds())  # type: ignore[attr-defined]
    assert scheduler.discover() == []  # finished: the next full cycle has a fresh budget
    assert seen[2] == ([], set()) and progresses[2] is not progresses[0]


def test_a_requested_discover_cut_by_github_unavailable_is_kept(sc: StageContext, ctx: AppContext) -> None:
    sc.gh, slept, _ = reader([status(502)], lambda: ctx.clock.now().timestamp())
    scheduler = ready(sc, ctx)
    assert scheduler.discover(["langfuse/langfuse", "o/r"]) == [] and sum(slept) <= 40
    assert scheduler.pending_discover == ["langfuse/langfuse", "o/r"] and scheduler.discover_after is not None


def test_a_plain_4xx_skips_only_that_target(sc: StageContext, ctx: AppContext) -> None:
    sc.gh, slept, seen = reader([status(403)])  # github_http_403: per target, the cycle goes on
    enabled = {t.repo for t in all_targets(ctx.config.targets) if t.enabled and t.discovery}
    scheduler = ready(sc, ctx)
    assert scheduler.discover() == [] and slept == []
    assert repos_read(seen) == enabled and scheduler.discover_after is None


def test_targets_verify_stops_reading_once_github_is_unavailable() -> None:
    gh, slept, seen = reader([REFUSED])
    rows = verify_targets.verify(gh, ["a/one", "b/two", "c/three"], 60, datetime.now(UTC))
    assert [r["status"] for r in rows] == ["FAIL"] * 3 and sum(slept) <= 40 and repos_read(seen) == {"a/one"}
    assert rows[0]["detail"].startswith("github_unavailable") and rows[2]["detail"] == "not checked: github_unavailable"
    gh403, _, seen403 = reader([status(403)])  # a plain 4xx is per repo
    verify_targets.verify(gh403, ["a/one", "b/two"], 60, datetime.now(UTC))
    assert repos_read(seen403) == {"a/one", "b/two"}


# ------------------------------------------- post-v1 open discovery: search methods and buckets (§7.4)
def recording(answer: Answer) -> tuple[GitHubRead, list[httpx.Request], list[float], Wall]:
    seen: list[httpx.Request] = []
    slept: list[float] = []
    wall = Wall()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer(request)

    client = github_read.make_client("t", transport=httpx.MockTransport(handler))
    return GitHubRead(client, sleep=slept.append, wall=wall, monotonic=wall), seen, slept, wall


def test_search_methods_are_get_only_with_exact_params() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/search/"):
            return httpx.Response(200, json={"total_count": 1, "items": [{"full_name": "o/r"}, "junk"]})
        if request.url.path.endswith("/commits"):
            return httpx.Response(200, json=[{"commit": {"committer": {"date": "2026-10-01T00:00:00Z"}}}])
        return httpx.Response(200, json={"files": {"contributing": None}})

    gh, seen, _, _ = recording(answer)
    q = "topic:gitops stars:>=1000 pushed:>=2026-08-22 archived:false mirror:false is:public"
    assert gh.search_repos(q) == [{"full_name": "o/r"}]
    assert gh.open_prs_mentioning("o/r", 7) == 1
    assert gh.community_profile("o/r") == {"files": {"contributing": None}}
    assert gh.default_branch_commit_at("o/r", "main") == "2026-10-01T00:00:00Z"
    gh.issues("o/r")
    assert {r.method for r in seen} == {"GET"}
    params = [dict(r.url.params) for r in seen]
    assert params[0] == {"q": q, "sort": "stars", "order": "desc", "per_page": "50"}
    assert params[1] == {"q": "repo:o/r is:pr is:open 7", "per_page": "1"}
    assert [r.url.path for r in seen[2:]] == ["/repos/o/r/community/profile", "/repos/o/r/commits",
                                              "/repos/o/r/issues"]  # fmt: skip
    assert params[3] == {"sha": "main", "per_page": "1"}
    assert params[4] == {"state": "open", "sort": "updated", "per_page": "100"}  # one page, no labels (AND)
    assert not hasattr(gh, "search_issues")  # discovery's issue search is gone (2026-10-22)
    for p in params:
        assert "advanced_search" not in p and "search_type" not in p


def test_pull_review_comments_is_one_get_with_since() -> None:
    """The watcher's inline review comments (§9.12): ``/pulls/{n}/comments``, ``since`` only when set."""
    gh, seen, _, _ = recording(lambda r: httpx.Response(200, json=[{"id": 1, "path": "a.py"}, "junk"]))
    assert gh.pull_review_comments("o/r", 9, "2026-10-23T10:00:00Z") == [{"id": 1, "path": "a.py"}]
    gh.pull_review_comments("o/r", 9)
    assert [(r.method, r.url.path, dict(r.url.params)) for r in seen] == [
        ("GET", "/repos/o/r/pulls/9/comments", {"per_page": "100", "since": "2026-10-23T10:00:00Z"}),
        ("GET", "/repos/o/r/pulls/9/comments", {"per_page": "100"}),
    ]


def test_community_profile_and_commit_date_404_read_as_none() -> None:
    gh, _, _, _ = recording(status(404))
    assert gh.community_profile("o/r") is None and gh.default_branch_commit_at("o/r", "main") is None


def test_a_search_limit_sets_only_the_search_bucket() -> None:
    limited = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(T0) + 300)}

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/search/"):
            return httpx.Response(403, json={"message": "API rate limit exceeded"}, headers=limited)
        return httpx.Response(200, json={"full_name": "o/r"})

    gh, seen, _, wall = recording(answer)
    with pytest.raises(GitHubRateLimited) as info:
        gh.search_repos("topic:x")
    assert info.value.until == T0 + 300 and gh.search_limited_until == T0 + 300 and gh.limited_until == 0.0
    with pytest.raises(GitHubRateLimited):
        gh.open_prs_mentioning("o/r", 1)  # every search waits, without a request
    assert len(seen) == 1
    assert gh.repo("o/r") == {"full_name": "o/r"} and len(seen) == 2  # core reads go on
    wall.t = T0 + 300
    with pytest.raises(GitHubRateLimited):
        gh.search_repos("topic:y")  # the reset passed: a request again (still limited here)
    assert len(seen) == 3


def test_x_ratelimit_resource_search_picks_the_search_bucket() -> None:
    headers = {"x-ratelimit-resource": "search", "retry-after": "30"}
    gh, _, _, _ = recording(lambda r: httpx.Response(429, json={"message": "x"}, headers=headers))
    with pytest.raises(GitHubRateLimited):
        gh.repo("o/r")
    assert (gh.search_limited_until, gh.limited_until) == (T0 + 30, 0.0)


def test_a_core_limit_does_not_block_searches() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/search/"):
            return httpx.Response(200, json={"total_count": 0, "items": []})
        return httpx.Response(429, json={"message": "x"}, headers={"retry-after": "60"})

    gh, seen, _, _ = recording(answer)
    with pytest.raises(GitHubRateLimited):
        gh.repo("o/r")
    assert gh.search_repos("topic:x") == [] and gh.limited_until == T0 + 60 and gh.search_limited_until == 0.0
    assert len(seen) == 2


def test_secondary_limit_backs_off_60_120_240_capped_at_900_and_resets() -> None:
    queue: list[int] = []

    def answer(request: httpx.Request) -> httpx.Response:
        if queue and queue.pop(0) == 200:
            return httpx.Response(200, json={"total_count": 0, "items": []})
        return httpx.Response(403, json={"message": "You have exceeded a Secondary Rate Limit. Please wait."})

    gh, _, _, wall = recording(answer)
    waits: list[float] = []
    for _ in range(6):
        queue.append(403)
        with pytest.raises(GitHubRateLimited) as info:
            gh.search_repos("topic:x")
        waits.append(info.value.until - wall.t)
        wall.t = info.value.until
    assert waits == [60.0, 120.0, 240.0, 480.0, 900.0, 900.0]
    queue.append(200)
    assert gh.search_repos("topic:x") == []  # a success resets the count
    queue.append(403)
    with pytest.raises(GitHubRateLimited) as info:
        gh.search_repos("topic:x")
    assert info.value.until - wall.t == 60.0


def test_retry_after_wins_over_the_secondary_backoff() -> None:
    gh, _, _, _ = recording(
        lambda r: httpx.Response(403, json={"message": "secondary rate limit"}, headers={"retry-after": "17"})
    )
    with pytest.raises(GitHubRateLimited) as info:
        gh.open_prs_mentioning("o/r", 1)
    assert info.value.until == T0 + 17


def test_a_plain_403_on_a_search_is_not_a_limit() -> None:
    gh, _, _, _ = recording(status(403))
    with pytest.raises(GitHubReadError) as info:
        gh.search_repos("topic:x")
    assert info.value.error_class == "github_http_403" and gh.search_limited_until == 0.0


def test_incomplete_results_are_used_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    doc = {"total_count": 9, "incomplete_results": True, "items": [{"full_name": "o/r"}]}
    gh, _, _, _ = recording(lambda r: httpx.Response(200, json=doc))
    with caplog.at_level("INFO", logger="ossbot.runner.github_read"):
        assert gh.search_repos("topic:x") == [{"full_name": "o/r"}]
    assert [r for r in caplog.records if getattr(r, "event", "") == "github_search_incomplete"]


def test_every_search_method_shares_the_gap() -> None:
    gh, seen, slept, wall = recording(lambda r: httpx.Response(200, json={"total_count": 0, "items": []}))
    gh.search_repos("topic:x")
    wall.t += 0.5
    gh.open_prs_mentioning("o/r", 2)
    wall.t += 3.0
    gh.open_prs_mentioning("o/r", 1)
    gh.search_repos("topic:y")
    assert len(seen) == 4 and slept == [pytest.approx(1.6), pytest.approx(2.1)]


# ------------------------------------------- discovery pacing (2026-10-22): the bounded search wait
SEARCH_OK = {"total_count": 1, "items": [{"full_name": "o/r"}]}


def limited_then(reset_in: float, *then: int) -> tuple[GitHubRead, list[httpx.Request], list[float], Wall]:
    """A search limit with ``x-ratelimit-reset`` ``reset_in`` s ahead, then the given statuses (200 = ok,
    403 = the same limit again), the last repeating."""
    queue = [403, *then]

    def answer(request: httpx.Request) -> httpx.Response:
        code = queue.pop(0) if len(queue) > 1 else queue[0]
        if code == 200:
            return httpx.Response(200, json=SEARCH_OK)
        headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(T0 + reset_in))}
        return httpx.Response(403, json={"message": "API rate limit exceeded"}, headers=headers)

    return recording(answer)


def checker(log_: list[str], slept: list[float]) -> Callable[[], None]:
    return lambda: log_.append(f"check@{len(slept)}")


def test_a_short_search_limit_is_waited_out_in_slices_with_checks() -> None:
    gh, seen, slept, _ = limited_then(50, 200)
    calls: list[str] = []
    assert gh.search_repos("topic:x", check=checker(calls, slept)) == [{"full_name": "o/r"}]
    assert len(seen) == 2  # the limited request, then the one after the wait
    assert slept[:-1] == [github_read.SEARCH_WAIT_SLICE_S] * 10  # 50 s in 5 s slices, outside the lock ...
    assert slept[-1] == pytest.approx(github_read.SEARCH_GAP_S)  # ... then the usual 2.1 s gap
    assert calls[0] == "check@0" and calls[-1] == "check@10" and len(calls) == 11  # before each slice, after the last


def test_a_known_short_limit_waits_before_the_request() -> None:
    gh, seen, slept, _ = recording(lambda r: httpx.Response(200, json={"total_count": 0, "items": []}))
    gh.search_limited_until = T0 + 30
    calls: list[str] = []
    assert gh.open_prs_mentioning("o/r", 3, check=checker(calls, slept)) == 0
    assert len(seen) == 1 and seen[0].url.params["q"] == "repo:o/r is:pr is:open 3"  # sent, GitHub decides
    assert slept == [5.0] * 6 and calls[0] == "check@0" and calls[-1] == "check@6"


def test_a_long_search_limit_raises_without_a_request_or_a_sleep() -> None:
    gh, seen, slept, _ = limited_then(120)
    with pytest.raises(GitHubRateLimited) as info:
        gh.search_repos("topic:x", check=lambda: None)
    assert info.value.until == T0 + 120 and len(seen) == 1 and slept == []
    with pytest.raises(GitHubRateLimited):
        gh.open_prs_mentioning("o/r", 1, check=lambda: None)  # known and > 65 s: no request at all
    assert len(seen) == 1 and slept == []


def test_a_stop_during_the_wait_propagates_and_sends_nothing() -> None:
    gh, seen, slept, _ = limited_then(50, 200)
    gh.search_limited_until = T0 + 50

    def check() -> None:
        if len(slept) == 3:
            raise Interrupted("paused")

    with pytest.raises(Interrupted):
        gh.search_repos("topic:x", check=check)
    assert seen == [] and len(slept) == 3


def test_a_second_limit_is_waited_once_more_then_a_third_raises() -> None:
    gh, seen, _, _ = limited_then(20, 403, 200)
    assert gh.search_repos("topic:x", check=lambda: None) == [{"full_name": "o/r"}]
    assert len(seen) == 3  # limit, wait, limit, wait, ok
    gh2, seen2, _, _ = limited_then(20, 403, 403, 200)
    with pytest.raises(GitHubRateLimited):
        gh2.search_repos("topic:x", check=lambda: None)
    assert len(seen2) == 3  # SEARCH_WAITS_MAX = 2 waits per call


def test_without_check_a_short_limit_still_raises_at_once() -> None:
    """Scout's mention check and ``ossctl targets search`` pass no check: today's behaviour."""
    gh, seen, slept, _ = limited_then(10, 200)
    with pytest.raises(GitHubRateLimited):
        gh.open_prs_mentioning("o/r", 1)
    with pytest.raises(GitHubRateLimited):
        gh.search_repos("topic:x")
    assert len(seen) == 1 and slept == [pytest.approx(github_read.SEARCH_GAP_S)]  # the gap only, no wait


def test_core_remaining_follows_core_answers_only() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/search/"):
            return httpx.Response(200, json=SEARCH_OK, headers={"x-ratelimit-remaining": "3"})
        headers = {"x-ratelimit-remaining": "812", "x-ratelimit-reset": str(int(T0) + 900)}
        return httpx.Response(200, json={"full_name": "o/r"}, headers=headers)

    gh, _, _, wall = recording(answer)
    assert gh.core_remaining is None and gh.core_left() is None
    gh.repo("o/r")
    assert (gh.core_remaining, gh.core_reset, gh.core_left()) == (812, T0 + 900, 812)
    gh.search_repos("topic:x")
    assert gh.core_remaining == 812  # a search answer is another bucket
    wall.t = T0 + 900
    assert gh.core_left() is None  # past its reset the number is stale
    junk, _, _, _ = recording(lambda r: httpx.Response(200, json={}, headers={"x-ratelimit-remaining": "lots"}))
    junk.repo("o/r")
    assert junk.core_remaining is None


def test_readme_and_directory_listing_on_the_fake() -> None:
    """Post-v1 vet reads (README, ``.github`` listing): GET only, 404 -> None / []."""
    fake = FakeGitHub()
    fake.add_repo("acme/widgets")
    fake.files[("acme/widgets", "README.md")] = "# Widgets\n"
    fake.files[("acme/widgets", ".github/PULL_REQUEST_TEMPLATE.md")] = "- [ ] tests\n"
    fake.files[("acme/widgets", ".github/workflows/ci.yml")] = "on: [pull_request]\n"
    gh = GitHubRead(fake.sync_client())
    assert gh.readme("acme/widgets") == "# Widgets\n"
    assert gh.readme("acme/other") is None
    entries = gh.dir_entries("acme/widgets", ".github")
    assert [(e["type"], e["name"], e["path"]) for e in entries] == [
        ("file", "PULL_REQUEST_TEMPLATE.md", ".github/PULL_REQUEST_TEMPLATE.md"),
        ("dir", "workflows", ".github/workflows"),
    ]
    assert gh.dir_entries("acme/widgets", "README.md") == []  # a file is not a directory
    assert gh.dir_entries("acme/widgets", "nope") == []
    assert gh.file_text("acme/widgets", ".github") is None  # a directory is not a file
    assert fake.writes == [] and {r.method for r in fake.requests} == {"GET"}


# ---------------------------------------- discovery fix 6: rate-limit headers in the log, GET /rate_limit
def test_a_rate_limit_answer_logs_the_ratelimit_headers(caplog: pytest.LogCaptureFixture) -> None:
    headers = {"x-ratelimit-limit": "30", "x-ratelimit-remaining": "0", "x-ratelimit-used": "30",
               "x-ratelimit-resource": "search", "x-ratelimit-reset": str(int(T0) + 40)}  # fmt: skip
    gh, _, _, _ = recording(lambda r: httpx.Response(403, json={"message": "API rate limit"}, headers=headers))
    with caplog.at_level("INFO", logger="ossbot.runner.github_read"), pytest.raises(GitHubRateLimited) as exc:
        gh.open_prs_mentioning("o/r", 1)
    assert exc.value.bucket == "search"
    (record,) = [r for r in caplog.records if getattr(r, "event", "") == "github_rate_limited"]
    fields = {k: getattr(record, k) for k in github_read.RATE_HEADERS}
    assert fields == {"limit": "30", "remaining": "0", "used": "30", "resource": "search", "reset": str(int(T0) + 40)}
    assert "limit 30, remaining 0, used 30, resource search" in record.getMessage()


def test_missing_ratelimit_headers_log_as_none_and_long_values_are_cut(caplog: pytest.LogCaptureFixture) -> None:
    headers = {"retry-after": "20", "x-ratelimit-resource": "core" + "x" * 100}
    gh, _, _, _ = recording(lambda r: httpx.Response(429, json={"message": "slow down"}, headers=headers))
    with caplog.at_level("INFO", logger="ossbot.runner.github_read"), pytest.raises(GitHubRateLimited) as exc:
        gh.repo("o/r")
    assert exc.value.bucket == "core"
    (record,) = [r for r in caplog.records if getattr(r, "event", "") == "github_rate_limited"]
    assert record.limit is None and record.used is None  # type: ignore[attr-defined]
    assert len(record.resource) == github_read.RATE_HEADER_MAX  # type: ignore[attr-defined]


def test_rate_limit_is_one_get_of_rate_limit() -> None:
    fake = FakeGitHub()
    doc = GitHubRead(fake.sync_client(), sleep=lambda s: None).rate_limit()
    assert doc is not None and doc["resources"]["search"]["limit"] == 30
    assert [(r.method, r.path, r.params) for r in fake.requests] == [("GET", "/rate_limit", {})]
