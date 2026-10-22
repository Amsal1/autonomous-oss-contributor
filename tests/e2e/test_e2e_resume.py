"""Crash safety end to end (AC 32, CR-001): the runner dies mid-implement (SIGKILL: no cleanup, its
stage run stays ``running``), a new runner starts: the run becomes ``interrupted``, the stage
re-queues with attempt 2, the item reaches the preview, and the approved submission writes exactly
once on the fake GitHub (no duplicate fork, push or PR)."""

from __future__ import annotations

from types import TracebackType
from typing import Any

import pytest

from ossbot.common.clock import to_iso
from ossbot.gatekeeper import db
from ossbot.runner import pipeline
from ossbot.runner.scheduler import Scheduler
from tests.e2e.conftest import E2E


def test_kill_then_resume_resends_a_fresh_card(e2e: E2E) -> None:
    """Post-v1 OD-C1: a kill invalidates the armed preview; nothing is re-sent while killed; after resume
    the gatekeeper's sweep (``reissue_due``, the first step of ``CardPublisher.recover``) sends one fresh
    card, which needs the full two-tap again and submits exactly once."""
    ctx = e2e.ctx
    cid, approval = e2e.to_preview()
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE approvals SET status = 'armed', armed_at = ? WHERE id = ?",
                         (to_iso(ctx.clock.now()), approval))  # fmt: skip
    ctx.control.kill("test", "tg:4242")
    assert ctx.reissue_due() == []
    ctx.control.request_resume("tg:4242")
    for flag in (ctx.paths.killed_flag, ctx.paths.paused_flag, ctx.paths.resume_request):
        flag.unlink(missing_ok=True)  # what the root enforcer does
    assert ctx.control.reconcile().state == "running"
    [fresh] = ctx.reissue_due()
    assert fresh != approval and e2e.pending_approval(cid) == fresh
    assert ctx.reissue_due() == []  # idempotent: nothing owed any more
    assert e2e.approve(fresh) == "dry_run_done"


class RunnerKilled(BaseException):
    """SIGKILL stand-in, raised inside the runner while its coder stage is open."""


class KilledOnCoder:
    def __init__(self, real: Any) -> None:
        self.real = real

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)

    def run(self, spec: dict[str, Any], **kw: Any) -> dict[str, Any]:
        if spec["entrypoint"] == "kiro" and spec["args"]["role"] == "coder":
            raise RunnerKilled
        return dict(self.real.run(spec, **kw))


def test_runner_killed_mid_implement_requeues_without_duplicate_writes(
    e2e: E2E, monkeypatch: pytest.MonkeyPatch
) -> None:
    e2e.ctx.control.set_dry_run(False, "uid:1000/operator")
    cid = e2e.discover_one()
    e2e.start_work(cid)
    assert e2e.run_until(cid, "PLANNED") == "PLANNED", e2e.why(cid)

    real_exit = pipeline.StageRun.__exit__

    def no_cleanup_on_kill(self: pipeline.StageRun, kind: type[BaseException] | None, exc: BaseException | None,
                           tb: TracebackType | None) -> None:  # fmt: skip
        if not isinstance(exc, RunnerKilled):  # a killed process runs no finally/__exit__
            real_exit(self, kind, exc, tb)

    monkeypatch.setattr(pipeline.StageRun, "__exit__", no_cleanup_on_kill)
    real_sbx = e2e.sc.sbx
    e2e.sc.sbx = KilledOnCoder(real_sbx)  # type: ignore[assignment]
    with pytest.raises(RunnerKilled):
        e2e.ready_scheduler().tick()
    runs = [tuple(r) for r in e2e.ctx.conn.execute("SELECT attempt, status FROM stage_runs WHERE cid = ?"
                                                    " AND stage = 'implement'", (cid,))]  # fmt: skip
    assert runs == [(1, "running")]

    e2e.sc.sbx = real_sbx  # a new runner process starts
    monkeypatch.setattr(pipeline.StageRun, "__exit__", real_exit)
    restarted = Scheduler(e2e.sc, synchronous=True, now=e2e.ctx.clock.now)
    assert restarted.recover_interrupted() == 1 and restarted.resumed == 1
    e2e.scheduler = restarted
    assert e2e.run_until(cid, "AWAITING_APPROVAL") == "AWAITING_APPROVAL", e2e.why(cid)
    runs = [tuple(r) for r in e2e.ctx.conn.execute("SELECT attempt, status FROM stage_runs WHERE cid = ?"
                                                    " AND stage = 'implement' ORDER BY attempt", (cid,))]  # fmt: skip
    assert runs == [(1, "interrupted"), (2, "succeeded")]
    assert e2e.approve(e2e.pending_approval(cid)) == "done"
    assert len(e2e.pusher.pushes) == 1
    assert sum(1 for w in e2e.writes() if w.method == "POST" and w.path.endswith("/pulls")) == 1
