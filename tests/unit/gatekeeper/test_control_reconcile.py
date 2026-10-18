"""Startup reconcile of flags and DB (N12) and the resume.log offset (N4)."""

from __future__ import annotations

import json

import pytest

from ossbot.common.clock import FakeClock
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalError
from ossbot.gatekeeper.context import AppContext
from tests.conftest import packaged_contribution, write_flag
from tests.unit.gatekeeper.test_approvals import card, confirm, status, tap


def set_db_state(ctx: AppContext, state: str, reason: str = "r") -> None:
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE control_state SET state = ?, reason = ? WHERE id = 1", (state, reason))


def events(ctx: AppContext, name: str) -> list[dict[str, object]]:
    rows = ctx.conn.execute("SELECT data_json FROM audit_events WHERE event = ? ORDER BY seq", (name,))
    return [json.loads(r["data_json"]) for r in rows]


def test_fresh_db_running_with_dry_run_on(ctx: AppContext) -> None:
    view = ctx.control.view()
    assert view.state == "running" and view.dry_run is True
    assert view.config_hash == ctx.snapshots.current.hash


def test_paused_file_at_start_means_paused(ctx: AppContext) -> None:
    write_flag(ctx.paths.paused_flag, {"reason": "watchdog: gatekeeper_unhealthy", "actor": "root", "ts": "t"})
    report = ctx.control.reconcile()
    assert report.state == "paused"
    row = db.read_control_row(ctx.conn)
    assert row is not None and row["state"] == "paused" and "gatekeeper_unhealthy" in row["reason"]


def test_killed_file_at_start_means_killed(ctx: AppContext) -> None:
    write_flag(ctx.paths.killed_flag, {"level": "full", "reason": "kill.ps1", "actor": "root", "ts": "t"})
    assert ctx.control.reconcile().state == "killed"
    assert ctx.control.view().kill_level == "full"


def test_db_paused_but_file_absent_rewrites_flag_and_alerts(ctx: AppContext) -> None:
    set_db_state(ctx, "paused", "anomaly")
    sent: list[str] = []
    ctx.control.notify = sent.append
    report = ctx.control.reconcile()
    assert report.state == "paused"
    assert ctx.paths.paused_flag.exists()
    assert "pause flag missing; use /resume" in report.alerts
    assert any("pause flag missing" in s for s in sent)


def test_db_killed_but_file_absent_stays_killed_with_alert(ctx: AppContext) -> None:
    set_db_state(ctx, "killed", "telegram")
    report = ctx.control.reconcile()
    assert report.state == "killed"
    assert "kill flag missing; use /resume" in report.alerts


def test_db_and_files_never_disagree_towards_running(ctx: AppContext) -> None:
    for state in ("paused", "killed"):
        set_db_state(ctx, state)
        assert ctx.control.reconcile().state != "running"


def test_resume_log_offline_entries_announced_once(ctx: AppContext, clock: FakeClock) -> None:
    set_db_state(ctx, "killed", "kill.ps1")
    sent: list[str] = []
    ctx.control.notify = sent.append
    line = {"ts": "2026-10-17T10:00:00Z", "actor_uid": 1000, "via": "resume.ps1", "reason": "done"}
    ctx.paths.resume_log.write_text(json.dumps(line) + "\n")
    report = ctx.control.reconcile()
    assert report.offline_resumes == 1 and report.state == "running"  # ossbot-resume removed the flags
    assert len(events(ctx, "operator_resume_offline")) == 1
    assert any("RESUME (offline, via resume.ps1)" in s for s in sent)
    row = db.read_control_row(ctx.conn)
    assert row is not None and row["resume_log_offset"] == len(json.dumps(line)) + 1
    # a restart does not announce it again
    assert ctx.control.reconcile().offline_resumes == 0
    assert len(events(ctx, "operator_resume_offline")) == 1


def test_resume_log_partial_line_not_consumed(ctx: AppContext) -> None:
    first = json.dumps({"ts": "a", "actor_uid": 0, "via": "ossctl"}) + "\n"
    ctx.paths.resume_log.write_text(first + '{"ts": "b", "act')
    assert ctx.control.reconcile().offline_resumes == 1
    with ctx.paths.resume_log.open("a") as fh:
        fh.write('or_uid": 0, "via": "ossctl"}\n')
    assert ctx.control.reconcile().offline_resumes == 1
    assert len(events(ctx, "operator_resume_offline")) == 2


def test_resume_log_rotation_resets_offset(ctx: AppContext) -> None:
    ctx.paths.resume_log.write_text(json.dumps({"ts": "a", "via": "x"}) + "\n" * 1 + json.dumps({"ts": "b"}) + "\n")
    assert ctx.control.reconcile().offline_resumes == 2
    ctx.paths.resume_log.write_text(json.dumps({"ts": "c"}) + "\n")  # shorter than the stored offset
    assert ctx.control.reconcile().offline_resumes == 1


def test_offline_resume_does_not_override_present_flags(ctx: AppContext) -> None:
    write_flag(ctx.paths.killed_flag, {"level": "soft", "reason": "x"})
    ctx.paths.resume_log.write_text(json.dumps({"ts": "a"}) + "\n")
    assert ctx.control.reconcile().state == "killed"


def _armed_and_confirmed(ctx: AppContext) -> tuple[str, str, str]:
    """(armed approval, its live confirm token, confirmed approval)."""
    _, a_armed = packaged_contribution(ctx)
    _, a_confirmed = packaged_contribution(ctx)
    armed, t1 = card(ctx, a_armed, message_id=11)
    confirm_token = tap(ctx, t1, message_id=11)
    confirmed, t2 = card(ctx, a_confirmed, message_id=12)
    confirm(ctx, tap(ctx, t2, message_id=12), message_id=12)
    return armed, confirm_token, confirmed


def test_root_written_soft_kill_while_running_invalidates_armed_and_confirmed(ctx: AppContext) -> None:
    """P1-2: watchdog / ossbot-kill write the flag directly; reconcile must apply §12.4."""
    armed, confirm_token, confirmed = _armed_and_confirmed(ctx)
    sent: list[str] = []
    ctx.control.notify = sent.append
    write_flag(ctx.paths.killed_flag, {"level": "soft", "reason": "watchdog: firewall_drift", "actor": "root"})
    assert ctx.control.reconcile().state == "killed"
    assert status(ctx, armed) == status(ctx, confirmed) == "invalidated"
    [event] = events(ctx, "control_reconciled")
    assert event["state"] == "killed" and event["level"] == "soft" and set(event["invalidated"]) == {armed, confirmed}
    assert any("2 armed/confirmed approval(s) invalidated" in s for s in sent)
    # an operator resume within the 5-minute TTL does not revive the confirm token
    ctx.paths.killed_flag.unlink()
    ctx.paths.resume_log.write_text(json.dumps({"ts": "a", "via": "resume.ps1"}) + "\n")
    assert ctx.control.reconcile().state == "running"
    with pytest.raises(ApprovalError):
        confirm(ctx, confirm_token, message_id=11)
    assert status(ctx, armed) == "invalidated"


def test_root_kill_then_request_resume_before_any_reconcile_invalidates(ctx: AppContext) -> None:
    """P1-7: a root kill resumed via /resume or ossctl resume inside one housekeeping tick."""
    armed, confirm_token, confirmed = _armed_and_confirmed(ctx)
    write_flag(ctx.paths.killed_flag, {"level": "soft", "reason": "ossbot-kill", "actor": "root"})
    ctx.control.request_resume("tg:4242")  # no reconcile ran in between
    assert status(ctx, armed) == status(ctx, confirmed) == "invalidated"
    [event] = events(ctx, "resume_requested")
    assert set(event["invalidated"]) == {armed, confirmed}
    # the enforcer removes the flags; the next reconcile resumes and the token stays dead
    ctx.paths.killed_flag.unlink()
    ctx.paths.resume_request.unlink()
    assert ctx.control.reconcile().state == "running"
    with pytest.raises(ApprovalError):
        confirm(ctx, confirm_token, message_id=11)


def test_request_resume_from_pause_keeps_approvals(ctx: AppContext) -> None:
    armed, _, confirmed = _armed_and_confirmed(ctx)
    ctx.control.pause("p", "tg:4242")
    ctx.control.request_resume("tg:4242")
    assert status(ctx, armed) == "armed" and status(ctx, confirmed) == "confirmed"


def test_root_kill_then_offline_resume_before_any_reconcile_invalidates(ctx: AppContext) -> None:
    """P1-7: kill flag written and removed (ossbot-resume --offline) between two reconciles."""
    armed, confirm_token, confirmed = _armed_and_confirmed(ctx)
    sent: list[str] = []
    ctx.control.notify = sent.append
    ctx.paths.resume_log.write_text(json.dumps({"ts": "a", "via": "resume.ps1", "killed": "{}"}) + "\n")
    report = ctx.control.reconcile()
    assert report.state == "running" and report.offline_resumes == 1
    assert status(ctx, armed) == status(ctx, confirmed) == "invalidated"
    with pytest.raises(ApprovalError):
        confirm(ctx, confirm_token, message_id=11)
    assert any("Offline resume observed: 2 armed/confirmed" in s for s in sent)
    [event] = events(ctx, "control_reconciled")
    assert set(event["invalidated"]) == {armed, confirmed}


def test_full_kill_flag_found_at_startup_invalidates(ctx: AppContext) -> None:
    armed, confirm_token, confirmed = _armed_and_confirmed(ctx)
    write_flag(ctx.paths.killed_flag, {"level": "full", "reason": "kill.ps1", "actor": "root", "ts": "t"})
    set_db_state(ctx, "killed", "kill.ps1")  # the DB already says killed: invalidation still happens
    assert ctx.control.reconcile().state == "killed"
    assert status(ctx, armed) == status(ctx, confirmed) == "invalidated"
    with pytest.raises(ApprovalError):
        confirm(ctx, confirm_token, message_id=11)


def test_repeated_reconcile_while_killed_is_quiet(ctx: AppContext) -> None:
    write_flag(ctx.paths.killed_flag, {"level": "soft", "reason": "x", "actor": "root"})
    ctx.control.reconcile()
    ctx.control.reconcile()
    assert len(events(ctx, "control_reconciled")) == 1


def test_enforcer_resume_after_request(ctx: AppContext) -> None:
    ctx.control.pause("test", "uid:1000/operator")
    ctx.control.request_resume("tg:4242")
    assert ctx.paths.resume_request.exists()
    assert ctx.control.reconcile().state == "paused"  # flags still there
    # root enforcer: removes paused and resume-request together
    ctx.paths.paused_flag.unlink()
    ctx.paths.resume_request.unlink()
    assert ctx.control.reconcile().state == "running"
    assert events(ctx, "resumed")
