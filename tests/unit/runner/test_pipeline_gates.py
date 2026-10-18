"""Stage gates (CR-001, AC 10): kill, pause, draining and down before each stage and at every
heartbeat; monotonic timeouts with sleep detection; stage-run bookkeeping; re-queue counting."""

from __future__ import annotations

import pytest

from ossbot.common.schemas import STAGE_ENTRYPOINTS, State
from ossbot.gatekeeper.context import AppContext
from ossbot.runner import stages
from ossbot.runner.llm import StageFailed
from ossbot.runner.pipeline import HEARTBEAT_S, STAGE_TIMEOUT_S, Interrupted, StageContext, StageRun
from ossbot.runner.scheduler import Scheduler
from tests.conftest import insert_contribution
from tests.unit.runner.conftest import FakeClocks


def stop(ctx: AppContext, how: str) -> None:
    if how == "paused":
        ctx.control.pause("test", "uid:1000/operator")
    elif how == "killed":
        ctx.control.kill("test", "uid:1000/operator")
    else:
        ctx.control.drain("uid:0/operator", "ossctl down", timeout_s=60, scope="distro")


def runs(ctx: AppContext, cid: str) -> list[tuple[str, str]]:
    return [(r["stage"], r["status"]) for r in ctx.conn.execute(
        "SELECT stage, status FROM stage_runs WHERE cid = ? ORDER BY started_at", (cid,))]  # fmt: skip


def test_every_stage_has_a_module() -> None:
    assert set(stages.STAGES) == set(STAGE_ENTRYPOINTS) == set(STAGE_TIMEOUT_S)


@pytest.mark.parametrize("stage", STAGE_ENTRYPOINTS)
@pytest.mark.parametrize("how", ["paused", "killed", "draining"])
def test_no_stage_starts_past_the_gate(sc: StageContext, ctx: AppContext, stage: str, how: str) -> None:
    cid = insert_contribution(ctx, State.VETTED)
    stop(ctx, how)
    with pytest.raises(Interrupted), StageRun(sc, cid, stage):
        raise AssertionError("must not run")
    assert runs(ctx, cid) == []


@pytest.mark.parametrize("how", ["paused", "killed"])
def test_heartbeat_interrupts_a_running_stage(sc: StageContext, ctx: AppContext, how: str) -> None:
    cid = insert_contribution(ctx, State.SCOUTING)

    def body() -> None:
        with StageRun(sc, cid, "scout") as run:
            stop(ctx, how)
            run.tick()

    with pytest.raises(Interrupted):
        body()
    assert runs(ctx, cid) == [("scout", "interrupted")]


def test_a_drain_lets_the_running_stage_finish(sc: StageContext, ctx: AppContext) -> None:
    """§13.5: draining/down stop new work only; the running stage completes."""
    cid = insert_contribution(ctx, State.SCOUTING)
    with StageRun(sc, cid, "scout") as run:
        stop(ctx, "draining")
        run.tick()
    assert runs(ctx, cid) == [("scout", "succeeded")]
    with pytest.raises(Interrupted), StageRun(sc, cid, "scout"):  # but no new stage starts
        raise AssertionError("must not run")


def test_heartbeat_every_15s_and_success(sc: StageContext, ctx: AppContext) -> None:
    clocks = sc.clocks
    assert isinstance(clocks, FakeClocks)
    cid = insert_contribution(ctx, State.FETCHING)
    with StageRun(sc, cid, "fetch") as run:
        clocks.advance(HEARTBEAT_S - 1)
        run.tick()
        first = ctx.conn.execute("SELECT heartbeat_at FROM stage_runs WHERE cid = ?", (cid,)).fetchone()[0]
        ctx.clock.advance(seconds=20)  # type: ignore[attr-defined]
        clocks.advance(2)
        run.tick()
        second = ctx.conn.execute("SELECT heartbeat_at FROM stage_runs WHERE cid = ?", (cid,)).fetchone()[0]
    assert second > first
    assert runs(ctx, cid) == [("fetch", "succeeded")]


def test_monotonic_timeout_fails_the_stage(sc: StageContext, ctx: AppContext) -> None:
    clocks = sc.clocks
    assert isinstance(clocks, FakeClocks)
    cid = insert_contribution(ctx, State.PLANNING)

    def body() -> None:
        with StageRun(sc, cid, "plan") as run:
            clocks.advance(STAGE_TIMEOUT_S["plan"] + 1)
            run.tick()

    with pytest.raises(StageFailed) as info:
        body()
    assert info.value.error_class == "timeout" and runs(ctx, cid) == [("plan", "failed")]


def test_host_sleep_is_an_interrupt_not_a_timeout(sc: StageContext, ctx: AppContext) -> None:
    clocks = sc.clocks
    assert isinstance(clocks, FakeClocks)
    cid = insert_contribution(ctx, State.PLANNING)

    def body() -> None:
        with StageRun(sc, cid, "plan") as run:
            clocks.advance(10, slept=4 * 3600)  # wall jumped 4 h, monotonic 10 s
            run.tick()

    with pytest.raises(Interrupted, match="slept"):
        body()
    assert runs(ctx, cid) == [("plan", "interrupted")]


def test_restart_requeues_interrupted_and_counts_resumed(sc: StageContext, ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.FETCHING)
    StageRun(sc, cid, "fetch").__enter__()  # a runner died mid-stage: the row stays running
    scheduler = Scheduler(sc, synchronous=True)
    assert scheduler.recover_interrupted() == 1 and scheduler.resumed == 1
    assert runs(ctx, cid) == [("fetch", "interrupted")]
    for _ in range(2):  # attempts 2 and 3 are allowed after interrupts, the 4th is refused
        with pytest.raises(Interrupted), StageRun(sc, cid, "fetch") as run:
            raise Interrupted("again")
        assert run.run_id
    with pytest.raises(StageFailed) as info, StageRun(sc, cid, "fetch"):
        pass
    assert info.value.error_class == "stage_open_requeue_limit"


def test_scheduler_starts_nothing_while_stopped(sc: StageContext, ctx: AppContext) -> None:
    insert_contribution(ctx, State.DISCOVERED)
    stop(ctx, "paused")
    assert Scheduler(sc, synchronous=True).tick().skipped == "stopped"
