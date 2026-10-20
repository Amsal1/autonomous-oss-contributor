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
from ossbot.runner.errors import Deferred, StageFailed
from ossbot.runner.github_read import GitHubRateLimited, GitHubRead, GitHubReadError
from ossbot.runner.pipeline import StageContext
from ossbot.runner.scheduler import DISCOVER_UNAVAILABLE_RETRY, Scheduler
from ossbot.runner.stages import discover
from tests.conftest import insert_contribution

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

    def fake_run(sc_: StageContext, *, repos: list[str] | None = None, refused: set[str],
                 created: list[str] | None = None) -> list[str]:  # fmt: skip
        assert created is not None
        seen.append((list(created), set(refused)))
        if len(seen) == 1:
            created.append("c1")
            refused.add("o/r#2")
            raise discover.GitHubUnavailable("down")
        return []

    monkeypatch.setattr(discover, "run", fake_run)
    scheduler = ready(sc, ctx)
    assert scheduler.discover() == []  # cut
    assert scheduler.discover() == []  # the re-run of the same cycle
    assert seen[1] == (["c1"], {"o/r#2"})
    assert scheduler.discover() == []  # finished: the next full cycle has a fresh budget
    assert seen[2] == ([], set())


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
