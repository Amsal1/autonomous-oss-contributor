"""FEAT-006 review round 2: back to gate 1 from RESCOPE_REQUIRED (Make it smaller / Split after a
ceiling or ``reapprove`` pause, F006-R2-1), stage errors in gate-owned states back off with one
owner message instead of a silent retry every tick, and the dispatch view covers every
non-terminal item however many terminal ones exist (F006-R2-2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.proposals import ProposalService
from ossbot.runner.pipeline import StageContext
from ossbot.runner.scheduler import HELD_RETRY, Scheduler
from ossbot.runner.stages import scout
from tests.conftest import insert_contribution
from tests.unit.gatekeeper.test_proposals import OWNER, sample
from tests.unit.gatekeeper.test_size_report import with_proposal
from tests.unit.runner.conftest import FakeSbx, fenced

SMALLER = sample(estimate={"files": 1, "code_lines": 8, "test_lines": 10, "class": "xs"})


def rescoped(ctx: AppContext, sc: StageContext, kind: str = "smaller") -> str:
    """An item paused in RESCOPE_REQUIRED at /package (rev 1 built), with the owner's feedback."""
    cid = with_proposal(ctx, approved="s")
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE contributions SET state = 'RESCOPE_REQUIRED', base_sha = ? WHERE id = ?",
                         ("1" * 40, cid))  # fmt: skip
    sc.save(cid, fetch={"has_test_suite": True, "base_sha": "1" * 40}, package_body={"rev": 1}, packaged={"rev": 1},
            diff="old diff", review={"verdict": "pass"}, claim_requested=True)  # fmt: skip
    ProposalService(ctx).request_revision(cid, 1, kind, "", OWNER)  # type: ignore[arg-type]
    return cid


def ready(sc: StageContext, ctx: AppContext) -> Scheduler:
    scheduler = Scheduler(sc, synchronous=True, now=ctx.clock.now)
    scheduler.first_start_done = True
    scheduler.last_discovery = scheduler.last_catalog = ctx.clock.now()
    return scheduler


def row(ctx: AppContext, cid: str) -> dict[str, Any]:
    found = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
    assert found is not None
    return dict(found)


# ------------------------------------------------------------------ F006-R2-1: re-scout from RESCOPE_REQUIRED
@pytest.mark.parametrize("kind", ["smaller", "split"])
def test_rescope_rescout_reads_the_build_tree_and_clones_nothing(
    sc: StageContext, ctx: AppContext, sbx: FakeSbx, kind: str
) -> None:
    sbx.kiro("scout", fenced(SMALLER))
    cid = rescoped(ctx, sc, kind)
    scheduler = ready(sc, ctx)
    assert cid in scheduler.tick().started
    assert [j["entrypoint"] for j in sbx.jobs] == ["kiro"]  # no scout clone into the build volume
    session = sbx.jobs[0]
    assert (session["args"]["role"], session["repo_mode"]) == ("scout", "snapshot")
    prompt = Path(session["inputs"]["scout-task.md"]).read_text(encoding="utf-8")
    assert "earlier attempt" in prompt and "Revise proposal v1" in prompt
    assert "list the tests you will add" in prompt  # the build's fetch.json says the repo has a suite
    after = row(ctx, cid)
    assert (after["state"], after["proposal_version"], after["proposal_feedback_json"]) == ("PROPOSED", 2, None)
    assert sbx.destroyed == [cid]  # PROPOSED: the old build tree goes, gate 1 waits without a volume
    notes = sc.load(cid)
    assert all(notes.get(k) is None for k in scout.BUILD_NOTES)  # the next Start work builds afresh
    assert notes["claim_requested"] is True  # the claim is about the issue, not the attempt


def test_failed_rescope_rescout_keeps_the_build_tree(sc: StageContext, ctx: AppContext, sbx: FakeSbx) -> None:
    sbx.handlers["kiro:scout"] = lambda spec, out: 1
    cid = rescoped(ctx, sc)
    with pytest.raises(Exception):  # noqa: B017 - StageFailed, handled by the scheduler
        scout.run(sc, sc.gk.contribution(cid))
    assert row(ctx, cid)["state"] == "RESCOPE_REQUIRED"
    assert sbx.destroyed == []  # Continue at the measured class still needs the build
    assert sc.load(cid)["package_body"] == {"rev": 1}


def test_stage_errors_in_a_gate_state_back_off_with_one_message(
    sc: StageContext, ctx: AppContext, sbx: FakeSbx
) -> None:
    said: list[str] = []
    sc.notify = said.append
    sbx.handlers["kiro:scout"] = lambda spec, out: 1
    cid = rescoped(ctx, sc)
    scheduler = ready(sc, ctx)

    def scouts() -> int:
        return sum(1 for j in sbx.jobs if j["entrypoint"] == "kiro")

    assert cid in scheduler.tick().started
    tries = scouts()
    assert tries >= 1 and row(ctx, cid)["state"] == "RESCOPE_REQUIRED"
    assert len(said) == 1 and "revising the proposal failed" in said[0] and "RESCOPE_REQUIRED" in said[0]
    for _ in range(5):  # within the backoff: nothing is re-dispatched
        ctx.clock.advance(seconds=20)  # type: ignore[attr-defined]
        assert cid not in scheduler.tick().started
    assert scouts() == tries
    ctx.clock.advance(seconds=HELD_RETRY.total_seconds())  # type: ignore[attr-defined]
    assert cid in scheduler.tick().started  # retried once the backoff is over
    assert scheduler.held[cid][0] == 2 and len(said) == 1  # the owner was told once
    held_until = scheduler.held[cid][1]
    assert held_until - ctx.clock.now() >= 2 * HELD_RETRY - HELD_RETRY / 10  # doubled
    sbx.kiro("scout", fenced(SMALLER))
    ctx.clock.advance(seconds=(2 * HELD_RETRY).total_seconds())  # type: ignore[attr-defined]
    assert cid in scheduler.tick().started
    assert row(ctx, cid)["state"] == "PROPOSED" and cid not in scheduler.held


# ------------------------------------------------------------------ F006-R2-2: the dispatch view
def test_dispatch_sees_old_items_behind_many_terminal_ones(
    sc: StageContext, ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = "2020-01-01T00:00:00Z"
    watching = insert_contribution(ctx, State.WATCHING, pr_number=7, updated_at=old, created_at=old)
    snoozed = insert_contribution(ctx, State.PROPOSAL_SNOOZED, snooze_until=old, updated_at=old, created_at=old)
    for _ in range(450):  # discovery leaves mostly SKIPPED candidates, all newer
        insert_contribution(ctx, State.SKIPPED)
    listed = {c["id"] for c in sc.gk.active_contributions(page=200)}
    assert listed == {watching, snoozed}
    assert len(sc.gk.contributions(limit=200)) == 200  # the plain list is still a window
    scheduler = ready(sc, ctx)
    ran: list[str] = []
    monkeypatch.setattr(scheduler, "run_one", ran.append)
    started = scheduler.tick().started
    assert set(started) == {watching, snoozed} and set(ran) == {watching, snoozed}


@pytest.mark.parametrize("how", ["sigterm", "pause"])
def test_discovery_ends_at_the_next_target_when_stopped(
    sc: StageContext, ctx: AppContext, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """A cycle reads for minutes; a SIGTERM (unit stop, TimeoutStopSec) or a kill/pause/drain ends
    it between targets instead of after the whole cycle, and the full cycle runs again later."""
    read: list[str] = []
    real_repo = sc.gh.repo

    def repo(name: str) -> Any:
        read.append(name)
        if how == "pause":
            ctx.control.pause("test", "uid:1000/operator")
        return real_repo(name)

    monkeypatch.setattr(sc.gh, "repo", repo)
    if how == "sigterm":
        sc.stopping = lambda: bool(read)
    scheduler = ready(sc, ctx)
    scheduler.last_discovery = None
    ran: list[str] = []
    monkeypatch.setattr(scheduler, "run_one", ran.append)
    out = scheduler.tick()
    assert len(read) == 1, read  # the next target was never read
    assert scheduler.last_discovery is None  # not counted as a finished cycle
    assert ran == [] and out.skipped == ("stopping" if how == "sigterm" else None)


def test_active_list_pages_past_the_page_size(sc: StageContext, ctx: AppContext) -> None:
    cids = {insert_contribution(ctx, State.PROPOSED) for _ in range(7)}
    for _ in range(3):
        insert_contribution(ctx, State.MERGED)
    assert {c["id"] for c in sc.gk.active_contributions(page=3)} == cids
