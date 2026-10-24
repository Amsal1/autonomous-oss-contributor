"""Scheduler and stage rules from FEAT-006 review round 1: sandbox interruptions re-queue instead
of failing (AC 32, CR-001), the M11 first-start order with the catalog_empty alert and the probe
gate held globally (§10.9, §20), the daily proposal cap held before scouting (§9.5b), re-scouts
only while still relevant, the claim guard for items without an issue, and the deterministic
"repo has a suite" of the tests-required rule (§9.14, AC 41)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from ossbot.common.models import LIVE_TEST_ROLE, Catalog
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.proposals import ProposalService
from ossbot.runner import scheduler as scheduler_mod
from ossbot.runner.llm import StageFailed
from ossbot.runner.outputs import ReviewVerdict
from ossbot.runner.pipeline import StageContext
from ossbot.runner.scheduler import Scheduler
from ossbot.runner.stages import common, fetch, review, scout
from tests.conftest import insert_contribution
from tests.fakes.fake_github import FakeGitHub
from tests.fakes.holds import hold_request
from tests.unit.gatekeeper.test_proposals import sample, start
from tests.unit.runner.conftest import MODELS, FakeSbx, fenced, fetch_json

ROOT = Path(__file__).resolve().parents[3]
VETTING = {"ai_policy": "allowed", "claim_status": "unclaimed", "suitability": 0.8}


def state(ctx: AppContext, cid: str) -> str:
    row = db.one(ctx.conn, "SELECT state FROM contributions WHERE id = ?", (cid,))
    assert row is not None
    return str(row["state"])


def runs(ctx: AppContext, cid: str) -> list[tuple[str, str]]:
    return [(r["stage"], r["status"]) for r in ctx.conn.execute(
        "SELECT stage, status FROM stage_runs WHERE cid = ? ORDER BY started_at, rowid", (cid,))]  # fmt: skip


def ready(sc: StageContext, ctx: AppContext) -> Scheduler:
    scheduler = Scheduler(sc, synchronous=True, now=ctx.clock.now)
    scheduler.last_discovery = scheduler.last_catalog = ctx.clock.now()
    return scheduler


def list_models_ok(spec: dict[str, Any], out: Path) -> int:
    (out / "models.json").write_text(json.dumps({"models": [{"model_id": m} for m in MODELS]}))
    return 0


# ------------------------------------------------------------------ R1-2: interruptions re-queue
@pytest.mark.parametrize("how", ["interrupted_job", "sandboxd_unreachable", "sandboxd_draining"])
def test_a_sandbox_interruption_requeues_instead_of_failing(
    sc: StageContext, ctx: AppContext, sbx: FakeSbx, how: str
) -> None:
    cid = insert_contribution(ctx, State.VETTED)
    if how == "interrupted_job":
        sbx.interrupt.add("fetch")  # sandboxd cut the job at the drain timeout / recovered it after a crash
    else:
        sbx.refuse["fetch"] = (503, "sandboxd_unreachable") if how == "sandboxd_unreachable" else (423, "draining")
    ready(sc, ctx).tick()
    assert state(ctx, cid) == "SCOUTING"  # not FAILED
    assert runs(ctx, cid) == [("scout", "interrupted")]
    sbx.interrupt.clear()
    sbx.refuse.clear()
    sbx.kiro("scout", fenced(sample()))
    ready(sc, ctx).tick()  # re-queued on the next tick, attempt 2
    assert state(ctx, cid) == "PROPOSED"
    assert runs(ctx, cid) == [("scout", "interrupted"), ("scout", "succeeded")]


def test_held_rows_are_not_dispatched_until_released(sc: StageContext, ctx: AppContext, sbx: FakeSbx) -> None:
    """Open-discovery §13.3: an active row with held: true is skipped; others still run; Resume frees it."""
    held = insert_contribution(ctx, State.VETTED)
    free = insert_contribution(ctx, State.VETTED)
    ctx.holds.hold(held, hold_request(stage="fetch", run_id="fetch-abc"), "uid:2002/sandbox")
    sbx.kiro("scout", fenced(sample()))
    started = ready(sc, ctx).tick().started
    assert held not in started and free in started
    assert runs(ctx, held) == [] and state(ctx, held) == "VETTED"
    ctx.holds.release(held, "tg:4242")
    assert held in ready(sc, ctx).tick().started
    assert runs(ctx, held) == [("scout", "succeeded")]


@pytest.mark.parametrize("how", ["job_error", "sandboxd_refused"])
def test_an_unknown_public_floor_defers_the_stage(sc: StageContext, ctx: AppContext, sbx: FakeSbx, how: str) -> None:
    """ADR-0025 (build-env design §4.7): never a failure or an interrupt; retried after RETRY_S, not next tick."""
    cid = insert_contribution(ctx, State.VETTED)
    if how == "job_error":
        sbx.handlers["fetch"] = lambda spec, out: 1
        sbx.error_class["fetch"] = "public_floor_unknown"
    else:
        sbx.refuse["fetch"] = (503, "public_floor_unknown")
    scheduler = ready(sc, ctx)
    scheduler.tick()
    assert state(ctx, cid) == "SCOUTING"  # not FAILED
    assert runs(ctx, cid) == [("scout", "cancelled")]
    assert cid in scheduler.delayed  # deferred: the next tick does not retry it at once
    assert cid not in scheduler.tick().started


@pytest.mark.parametrize("how", ["job_error", "sandboxd_pending"])
def test_an_egress_anomaly_is_not_a_stage_failure(sc: StageContext, ctx: AppContext, sbx: FakeSbx, how: str) -> None:
    cid = insert_contribution(ctx, State.VETTED)
    if how == "job_error":  # the scout's depth-1 clone is a fetch job, metered like any fetch
        sbx.handlers["fetch"] = lambda spec, out: 1
        sbx.error_class["fetch"] = "egress_anomaly"
    else:
        sbx.refuse["fetch"] = (423, "egress_anomaly_pending")
    ready(sc, ctx).tick()
    assert state(ctx, cid) == "SCOUTING"  # not FAILED
    assert runs(ctx, cid) == [("scout", "cancelled")]


def test_the_kiro_job_interrupted_by_sandboxd_requeues(sc: StageContext, ctx: AppContext, sbx: FakeSbx) -> None:
    cid = insert_contribution(ctx, State.DISCOVERED)
    sbx.interrupt.add("kiro")
    ready(sc, ctx).tick()
    assert state(ctx, cid) == "VETTING" and runs(ctx, cid) == [("vet", "interrupted")]


# ------------------------------------------------------------------ R1-3: M11 first start
def test_first_start_order_catalog_then_probes_then_stages(sc: StageContext, ctx: AppContext, sbx: FakeSbx) -> None:
    ctx.conn.execute("DELETE FROM model_catalog")
    sbx.handlers["list_models"] = list_models_ok
    sbx.probes_passed = False
    sbx.kiro("vetter", fenced(VETTING))
    cid = insert_contribution(ctx, State.DISCOVERED)
    scheduler = Scheduler(sc, synchronous=True, now=ctx.clock.now)
    scheduler.last_discovery = ctx.clock.now()
    scheduler.tick()
    assert sbx.calls[:4] == ["list_models", "probes_status", "probes_run", "probes_status"]
    assert sbx.calls[4] == "kiro:vetter"  # the first stage only after the probes passed
    test_model = sc.models.resolve(LIVE_TEST_ROLE, Catalog.from_rows(sc.gk.catalog())).model
    assert sbx.probe_runs == [{"planner": test_model, "coder": test_model}]  # roles.test, never planner/coder
    assert state(ctx, cid) == "VETTED"


def test_catalog_empty_alerts_once_retries_hourly_and_runs_nothing(
    sc: StageContext, ctx: AppContext, sbx: FakeSbx
) -> None:
    said: list[str] = []
    sc.notify = said.append
    ctx.conn.execute("DELETE FROM model_catalog")
    sbx.handlers["list_models"] = lambda spec, out: 2
    cid = insert_contribution(ctx, State.DISCOVERED)
    scheduler = Scheduler(sc, synchronous=True, now=ctx.clock.now)
    for _ in range(3):
        assert scheduler.tick().skipped == "not_ready"
    assert sbx.calls == ["list_models"]  # no probe, no Kiro job, one try per hour
    assert len(said) == 1 and "catalog_empty" in said[0]
    assert state(ctx, cid) == "DISCOVERED" and runs(ctx, cid) == []
    ctx.clock.advance(seconds=3601)  # type: ignore[attr-defined]
    sbx.handlers["list_models"] = list_models_ok
    sbx.kiro("vetter", fenced(VETTING))
    scheduler.last_discovery = ctx.clock.now()
    scheduler.tick()
    assert sbx.calls[1] == "list_models" and state(ctx, cid) == "VETTED"


def test_probe_gate_refusal_holds_everything_and_reruns_the_probes(
    sc: StageContext, ctx: AppContext, sbx: FakeSbx
) -> None:
    items = [insert_contribution(ctx, State.DISCOVERED) for _ in range(3)]
    scheduler = ready(sc, ctx)
    scheduler.first_start_done = True
    sbx.probes_passed = False  # e.g. a new image tuple after a rebuild
    sbx.probes_pass_on_run = False
    scheduler.tick()
    assert sbx.calls.count("kiro:vetter") == 1  # the first refusal stops the dispatch of the others
    assert {state(ctx, c) for c in items} <= {"DISCOVERED", "VETTING"}  # nothing FAILED
    assert [s for c in items for s in runs(ctx, c)] == [("vet", "cancelled")]  # refused = not started
    for _ in range(5):  # probes do not pass: retried after PROBE_RETRY, not every tick
        assert scheduler.tick().skipped == "not_ready"
    assert sbx.calls.count("probes_run") == 1 and sbx.calls.count("kiro:vetter") == 1
    sbx.probes_pass_on_run = True
    sbx.kiro("vetter", fenced(VETTING))
    ctx.clock.advance(seconds=31 * 60)  # type: ignore[attr-defined]
    scheduler.tick()
    assert {state(ctx, c) for c in items} == {"VETTED"}


# ------------------------------------------------------------------ R1-6: daily cap before scouting
def test_daily_cap_holds_autonomous_scouting_without_cloning(sc: StageContext, ctx: AppContext, sbx: FakeSbx) -> None:
    cap = ctx.config.policy.proposal.max_per_day
    sbx.kiro("scout", fenced(sample()))
    for _ in range(cap):
        scout.run(sc, sc.gk.contribution(insert_contribution(ctx, State.VETTED)))
    pacing = sc.gk.pacing()
    assert pacing["remaining"] == 0 and pacing["next_slot_at"] is not None
    waiting = insert_contribution(ctx, State.VETTED)
    user = insert_contribution(ctx, State.VETTED, source="user")
    jobs_before = len(sbx.jobs)
    scheduler = ready(sc, ctx)
    scheduler.first_start_done = True
    scheduler.tick()
    assert state(ctx, waiting) == "VETTED" and runs(ctx, waiting) == []  # no transition, no clone, no session
    assert state(ctx, user) == "PROPOSED"  # 👤 items are not counted
    assert len(sbx.jobs) == jobs_before + 2  # only the 👤 item's clone and scout session
    scheduler.tick()
    assert state(ctx, waiting) == "VETTED"
    ctx.clock.advance(seconds=86_401)  # type: ignore[attr-defined]
    scheduler.tick()
    assert state(ctx, waiting) == "PROPOSED"


# ------------------------------------------------------------------ R1-8: re-scout only if relevant
@pytest.mark.parametrize(
    ("issue", "from_state", "expected", "reason"),
    [
        ({"state": "closed"}, State.PROPOSAL_EXPIRED, "SKIPPED", "no_longer_relevant:issue_closed"),
        ({"assignees": [{"login": "someone"}]}, State.PROPOSAL_SNOOZED, "SKIPPED",
         "no_longer_relevant:issue_assigned"),
        ({}, State.PROPOSAL_EXPIRED, "PROPOSED", None),
    ],
)  # fmt: skip
def test_rescout_only_while_still_relevant(
    sc: StageContext, ctx: AppContext, sbx: FakeSbx, github: FakeGitHub, issue: dict[str, Any], from_state: State,
    expected: str, reason: str | None,
) -> None:  # fmt: skip
    github.add_issue("langfuse/langfuse", 501, title="Bug", body="b", **issue)
    cid = insert_contribution(ctx, State.SCOUTING, issue_number=501)
    sbx.kiro("scout", fenced(sample()))
    scout.run(sc, sc.gk.contribution(cid))
    ctx.conn.execute("UPDATE contributions SET state = ? WHERE id = ?", (from_state.value, cid))
    clones = sum(1 for j in sbx.jobs if j["entrypoint"] == "fetch")
    scout.run(sc, sc.gk.contribution(cid))
    row = db.one(ctx.conn, "SELECT state, state_reason FROM contributions WHERE id = ?", (cid,))
    assert row is not None and row["state"] == expected
    if reason is not None:
        assert row["state_reason"] == reason
        assert sum(1 for j in sbx.jobs if j["entrypoint"] == "fetch") == clones  # nothing re-scouted


# ------------------------------------------------------------------ R1-9: claim guard without an issue
def test_claim_guard_does_not_hold_items_without_an_issue(sc: StageContext, ctx: AppContext, sbx: FakeSbx) -> None:
    ctx.state_machine.claim_comment = lambda: True
    cid = insert_contribution(ctx, State.SCOUTING, issue_number=None, source="user", work_ref="repo",
                              work_url="https://github.com/langfuse/langfuse")  # fmt: skip
    sbx.kiro("scout", fenced(sample()))
    scout.run(sc, sc.gk.contribution(cid))
    pid = db.one(ctx.conn, "SELECT id FROM proposal_approvals WHERE cid = ?", (cid,))[0]
    start(ctx, pid)
    fetch.run(sc, sc.gk.contribution(cid))
    assert state(ctx, cid) == "FETCHED"


# ------------------------------------------------------------------ R1-7: deterministic "has a suite"
def test_test_path_pattern_matches_fetch_sh() -> None:
    text = (ROOT / "sandbox" / "entrypoints" / "fetch.sh").read_text(encoding="utf-8")
    m = re.search(r"^TEST_RE='([^']+)'$", text, re.MULTILINE)
    assert m is not None and m.group(1) == common.TEST_PATH_RE.pattern
    hits = ["tests/test_a.py", "pkg/a_test.go", "web/x.test.ts", "spec/a_spec.rb", "pytest.ini", "src/conftest.py"]
    assert all(common.TEST_PATH_RE.search(p) for p in hits)
    assert not any(common.TEST_PATH_RE.search(p) for p in ["src/a.py", "README.md", "latest/x.go", "contest.py"])


@pytest.mark.parametrize(("info", "expected"), [(None, True), ({}, True), ({"has_test_suite": False}, False),
                                                ({"has_test_suite": True}, True)])  # fmt: skip
def test_has_test_suite_fails_closed(info: dict[str, Any] | None, expected: bool) -> None:
    assert common.has_test_suite(info) is expected


def test_review_ignores_the_scouts_no_suite_claim(sc: StageContext, ctx: AppContext, sbx: FakeSbx) -> None:
    """The scout said tests: [] with a reason, but fetch.json found a suite: tests are still required."""
    cid = insert_contribution(ctx, State.VALIDATED, review_rounds=0)
    sc.save(cid, diff="diff --git a/src/a.py b/src/a.py\n+x\n", fetch={"has_test_suite": True})
    sbx.kiro("reviewer", fenced(ReviewVerdict(verdict="pass").model_dump()))
    review.run(sc, sc.gk.contribution(cid))
    assert state(ctx, cid) == "REVISE"
    assert sc.load(cid)["review"]["blocking"][-1]["issue"] == review.TESTS_MISSING
    other = insert_contribution(ctx, State.VALIDATED, review_rounds=0)
    sc.save(other, diff="diff --git a/src/a.py b/src/a.py\n+x\n", fetch={"has_test_suite": False})
    review.run(sc, sc.gk.contribution(other))
    assert state(ctx, other) == "REVIEW_PASSED" and sc.load(other)["review"]["tests_exception"] == "no_suite"


def test_scout_must_list_tests_when_the_repo_has_a_suite(sc: StageContext, ctx: AppContext, sbx: FakeSbx) -> None:
    no_tests = sample(tests=[], no_suite_reason="there are no tests")
    sbx.kiro("scout", fenced(no_tests), fenced(sample()))  # one repair retry fixes it
    cid = insert_contribution(ctx, State.VETTED)
    scout.run(sc, sc.gk.contribution(cid))
    assert state(ctx, cid) == "PROPOSED"
    tasks = [j for j in sbx.jobs if j["entrypoint"] == "kiro"]
    assert len(tasks) == 2 and "must list the tests" in Path(tasks[1]["inputs"]["scout-task.md"]).read_text()
    sbx.kiro("scout", fenced(no_tests))
    again = insert_contribution(ctx, State.VETTED)
    with pytest.raises(StageFailed) as info:
        scout.run(sc, sc.gk.contribution(again))
    assert info.value.error_class == "output_invalid"
    sbx.handlers["fetch"] = lambda spec, out: fetch_json(spec, out, has_test_suite=False)
    fine = insert_contribution(ctx, State.VETTED)
    scout.run(sc, sc.gk.contribution(fine))  # no suite: the reason is accepted
    assert state(ctx, fine) == "PROPOSED"


def test_pacing_reports_the_next_slot(ctx: AppContext) -> None:
    svc = ProposalService(ctx)
    assert svc.pacing() == {"max_per_day": ctx.config.policy.proposal.max_per_day, "used": 0,
                            "remaining": ctx.config.policy.proposal.max_per_day, "next_slot_at": None}  # fmt: skip


def test_ossctl_discover_request_runs_one_cycle(
    sc: StageContext, ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str] | None] = []
    monkeypatch.setattr(
        scheduler_mod.discover, "run", lambda sc, repos=None, refused=None, created=None, **kw: seen.append(repos) or []
    )  # **kw: now and progress (discovery pacing, 2026-10-22)
    scheduler = ready(sc, ctx)
    scheduler.first_start_done = True
    scheduler.tick()
    assert seen == []  # interval not due, nothing requested
    ctx.contributions.request_discover(["langfuse/langfuse"], "uid:1000/operator")
    scheduler.tick()
    scheduler.tick()  # consumed once
    assert seen == [["langfuse/langfuse"]]


def test_reviewer_collision_pauses_instead_of_reviewing_with_the_coders_model(
    sc: StageContext, ctx: AppContext, sbx: FakeSbx
) -> None:
    """Owner rule: require_distinct_from holds after fallback; no catalog model distinct from the
    coder's -> pause with an alert and re-queue, never a review by the coder's own model."""
    ctx.conn.execute("DELETE FROM model_catalog WHERE model_id != 'claude-opus-5.5'")
    cid = insert_contribution(ctx, State.VALIDATED, review_rounds=0)
    sc.save(cid, diff="diff --git a/src/a.py b/src/a.py\n+x\n", fetch={"has_test_suite": True})
    ready(sc, ctx).tick()
    assert ctx.control.view().state == "paused"
    assert not [j for j in sbx.jobs if j["entrypoint"] == "kiro"]
    assert state(ctx, cid) == "REVIEWING" and runs(ctx, cid) == [("review", "interrupted")]


def test_items_that_only_ran_kiro_jobs_get_their_networks_released(
    sc: StageContext, ctx: AppContext, sbx: FakeSbx
) -> None:
    """Live finding: vetted-and-skipped items kept their per-task research networks (the /28 pool
    leaks). Every cid that ran a sandbox job is noted; the sweep destroys it once terminal."""
    sbx.kiro("vetter", fenced({**VETTING, "ai_policy": "banned"}))  # a skip (unclear is allowed post-v1)
    cid = insert_contribution(ctx, State.DISCOVERED)
    scheduler = ready(sc, ctx)
    scheduler.tick()
    assert state(ctx, cid) == "SKIPPED"
    scheduler.tick()
    assert cid in sbx.destroyed and cid not in common.volume_cids(sc)
