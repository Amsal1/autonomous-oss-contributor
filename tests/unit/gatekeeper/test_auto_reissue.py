"""Post-v1 OD-C1 (DESIGN §12.4, §13.2, §14.18.4.4; ADR-0008 addendum): after any invalidation that leaves
the subject eligible, ``AppContext.reissue_due`` (run first in ``CardPublisher.recover``) re-sends a fresh
card, the same way ``reissue_cards`` does for a reload or dry-run toggle. Covers a reload, a start-time
snapshot change, kill -> resume and pause -> resume; nothing (and no decision token) while killed or
paused; idempotent across restarts; the binding is recomputed (I1, I3 unchanged)."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from ossbot.common.clock import FakeClock, to_iso
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalError, binding_hash, compute_binding
from ossbot.gatekeeper.context import AppContext, build_context
from ossbot.gatekeeper.outbox import OutboxWorker
from ossbot.gatekeeper.telegram.bot import CardPublisher
from tests.conftest import TEST_ROLES, FakeArtifacts, packaged_contribution
from tests.fakes.holds import hold_request
from tests.unit.gatekeeper.decision_helpers import (
    CHAT,
    RecordingSender,
    install_sources,
    proposal_card,
    proposed_contribution,
    skill_card,
)

DECISION = ("approve", "reject", "reject_reason", "changes", "confirm", "cancel", "proposal_start",
            "proposal_confirm", "proposal_cancel", "skill_approve", "skill_confirm", "skill_cancel",
            "skill_reject")  # fmt: skip


# ------------------------------------------------------------------------------ helpers
def _wire(ctx: AppContext) -> CardPublisher:
    publisher = CardPublisher(ctx)
    ctx.on_approval_created = publisher.publish
    ctx.card_recover = publisher.recover
    return publisher


async def _deliver(ctx: AppContext, publisher: CardPublisher) -> None:
    worker = OutboxWorker(ctx.outbox, RecordingSender(), publisher.on_sent, batch=50)
    for _ in range(20):
        if await worker.run_once() == 0 and not ctx.outbox.due(1):
            break


async def _tick(ctx: AppContext, publisher: CardPublisher) -> None:
    publisher.recover()
    await _deliver(ctx, publisher)


async def _card(ctx: AppContext, publisher: CardPublisher) -> tuple[str, str, str]:
    """(cid, action, approval) of a delivered and armed gate-2 card."""
    cid, action = packaged_contribution(ctx)
    with db.transaction(ctx.conn):
        approval = ctx.create_approval(action, "test")
    ctx.on_approval_created(approval)
    await _deliver(ctx, publisher)
    assert db.one(ctx.conn, "SELECT 1 FROM tg_outbox WHERE dedupe_key = ?", (f"arm:{approval}",)) is not None
    return cid, action, approval


def _open(ctx: AppContext, action: str) -> list[Any]:
    return list(ctx.conn.execute("SELECT * FROM approvals WHERE action_id = ? AND status IN ('pending','armed')",
                                 (action,)).fetchall())  # fmt: skip


def _status(ctx: AppContext, table: str, approval: str) -> tuple[str, str | None]:
    row = db.one(ctx.conn, f"SELECT status, invalidated_reason FROM {table} WHERE id = ?", (approval,))
    assert row is not None
    return str(row[0]), row[1]


def _decision_tokens(ctx: AppContext) -> int:
    marks = ",".join("?" * len(DECISION))
    return int(ctx.conn.execute(f"SELECT COUNT(*) FROM ui_tokens WHERE purpose IN ({marks})", DECISION).fetchone()[0])


def _resume(ctx: AppContext) -> None:
    ctx.control.request_resume("tg:4242")
    for flag in (ctx.paths.killed_flag, ctx.paths.paused_flag, ctx.paths.resume_request):
        flag.unlink(missing_ok=True)  # what the root enforcer does
    assert ctx.control.reconcile().state == "running"


def _touch_config(ctx: AppContext) -> None:
    path = ctx.paths.config_dir / "telegram.yaml"
    path.write_bytes(path.read_bytes() + b"\n# changed by a test\n")


def _events(ctx: AppContext, event: str) -> list[dict[str, Any]]:
    return [json.loads(r[0]) for r in ctx.conn.execute("SELECT data_json FROM audit_events WHERE event = ?"
                                                         " ORDER BY seq", (event,))]  # fmt: skip


# ------------------------------------------------------------------------------ (a) reload
async def test_reload_while_running_sends_one_fresh_card(ctx: AppContext) -> None:
    publisher = _wire(ctx)
    _, action, old = await _card(ctx, publisher)
    _touch_config(ctx)
    preview = ctx.snapshots.prepare_reload()
    _, invalidated = ctx.snapshots.apply_reload(preview.hash, "tg:4242")
    created = ctx.reissue_cards(invalidated, "tg:4242")
    assert len(created) == 1 and _status(ctx, "approvals", old) == ("invalidated", "config_snapshot_changed")
    await _tick(ctx, publisher)
    assert [r["id"] for r in _open(ctx, action)] == created  # the sweep adds nothing


# ------------------------------------------------------------------------- (b) deferred reload
@pytest.mark.parametrize("stop", ["kill", "pause"])
async def test_reload_while_stopped_is_deferred_until_resume(ctx: AppContext, stop: str) -> None:
    publisher = _wire(ctx)
    _, action, _ = await _card(ctx, publisher)
    if stop == "kill":
        ctx.control.kill("test", "tg:4242")
    else:
        ctx.control.pause("test", "tg:4242")
    _touch_config(ctx)
    preview = ctx.snapshots.prepare_reload()
    _, invalidated = ctx.snapshots.apply_reload(preview.hash, "tg:4242")
    before = int(ctx.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0])
    assert ctx.reissue_cards(invalidated, "tg:4242") == []
    await _tick(ctx, publisher)
    assert int(ctx.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]) == before
    _resume(ctx)
    await _tick(ctx, publisher)
    [fresh] = _open(ctx, action)
    assert db.one(ctx.conn, "SELECT 1 FROM tg_outbox WHERE dedupe_key = ?", (f"arm:{fresh['id']}",)) is not None


# ------------------------------------------------------------------ (c) start-time snapshot change
async def test_start_time_snapshot_change_resends_once_across_restarts(ctx: AppContext, clock: FakeClock,
                                                                       artifacts: FakeArtifacts) -> None:  # fmt: skip
    publisher = _wire(ctx)
    _, action, old = await _card(ctx, publisher)
    _touch_config(ctx)
    for _ in range(4):  # the first start sees the change; the next three restarts see the same config
        restarted = build_context(ctx.paths, TEST_ROLES, clock=clock, artifacts=artifacts, conn=ctx.conn)
        restarted.snapshots.load_at_start()
        restarted.control.reconcile()
        fresh_publisher = _wire(restarted)
        await _tick(restarted, fresh_publisher)
        assert len(_open(restarted, action)) == 1
    assert _status(ctx, "approvals", old) == ("invalidated", "config_snapshot_changed")
    keys = [r[0] for r in ctx.conn.execute("SELECT dedupe_key FROM tg_outbox WHERE dedupe_key LIKE 'card:%'")]
    assert len(keys) == 2 == len(set(keys))  # the original and one fresh card, no duplicates
    assert len(_events(ctx, "auto_reissue")) == 1


# --------------------------------------------------------------------------- (d) kill -> resume
async def test_kill_then_resume_mints_nothing_while_killed(ctx: AppContext) -> None:
    publisher = _wire(ctx)
    _, action, old = await _card(ctx, publisher)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE approvals SET status = 'armed', armed_at = ? WHERE id = ?",
                         (to_iso(ctx.clock.now()), old))  # fmt: skip
    ctx.control.kill("test", "tg:4242")
    assert _status(ctx, "approvals", old) == ("invalidated", "killed")
    tokens = _decision_tokens(ctx)
    for _ in range(3):
        await _tick(ctx, publisher)
    assert _open(ctx, action) == [] and _decision_tokens(ctx) == tokens  # no card, no decision token
    _resume(ctx)
    await _tick(ctx, publisher)
    [fresh] = _open(ctx, action)
    assert fresh["status"] == "pending" and fresh["expires_at"] == db.one(
        ctx.conn, "SELECT expires_at FROM approvals WHERE id = ?", (old,))[0]  # the window is kept  # fmt: skip
    assert db.one(ctx.conn, "SELECT 1 FROM tg_outbox WHERE dedupe_key = ?", (f"arm:{fresh['id']}",)) is not None
    assert _decision_tokens(ctx) > tokens


async def test_card_created_while_paused_is_armed_only_after_resume(ctx: AppContext) -> None:
    """Defence in depth (D5): whatever path creates a card while paused, ``_maybe_arm`` mints nothing."""
    publisher = _wire(ctx)
    ctx.control.pause("test", "tg:4242")
    _, action = packaged_contribution(ctx)
    with db.transaction(ctx.conn):
        approval = ctx.create_approval(action, "test")
    ctx.on_approval_created(approval)
    tokens = _decision_tokens(ctx)
    await _tick(ctx, publisher)
    assert db.one(ctx.conn, "SELECT 1 FROM tg_outbox WHERE dedupe_key = ?", (f"arm:{approval}",)) is None
    assert _decision_tokens(ctx) == tokens
    _resume(ctx)
    await _tick(ctx, publisher)
    assert db.one(ctx.conn, "SELECT 1 FROM tg_outbox WHERE dedupe_key = ?", (f"arm:{approval}",)) is not None


# ---------------------------------------------------------------- (e) pause + dry-run toggle, (h)
async def test_pause_and_toggle_then_resume_rebinds_to_the_current_mode(ctx: AppContext) -> None:
    publisher = _wire(ctx)
    _, action, old = await _card(ctx, publisher)
    ctx.control.pause("test", "tg:4242")
    invalidated = ctx.control.set_dry_run(False, "tg:4242")
    assert ctx.reissue_cards(invalidated, "tg:4242") == []
    await _tick(ctx, publisher)
    assert _open(ctx, action) == []
    _resume(ctx)
    await _tick(ctx, publisher)
    [fresh] = _open(ctx, action)
    binding = compute_binding(ctx.conn, action, ctx.approvals.artifacts)
    assert binding["dry_run"] is False and fresh["dry_run_shown"] == 0
    assert fresh["binding_hash"] == binding_hash(binding) != db.one(
        ctx.conn, "SELECT binding_hash FROM approvals WHERE id = ?", (old,))[0]  # fmt: skip


# ------------------------------------------------------------------- (f) proposal and skill cards
async def test_proposal_and_skill_cards_follow_the_same_rule(ctx: AppContext) -> None:
    _, skills = install_sources(ctx)
    skills.staged["git-helper"] = "sr_1"
    publisher = _wire(ctx)
    cid = proposed_contribution(ctx)
    pid, _ = proposal_card(ctx, cid)
    sid, _ = skill_card(ctx)
    ctx.control.kill("test", "tg:4242")
    with db.transaction(ctx.conn):  # what a start-time snapshot change does to all three services
        ctx.decisions.invalidate_all_open("config_snapshot_changed", "system")
    await _tick(ctx, publisher)
    assert db.one(ctx.conn, "SELECT 1 FROM proposal_approvals WHERE status = 'pending'") is None
    assert db.one(ctx.conn, "SELECT 1 FROM skill_approvals WHERE status = 'pending'") is None
    _resume(ctx)
    await _tick(ctx, publisher)
    [p] = ctx.conn.execute("SELECT id FROM proposal_approvals WHERE cid = ? AND status = 'pending'", (cid,)).fetchall()
    [s] = ctx.conn.execute(
        "SELECT id FROM skill_approvals WHERE skill = 'git-helper' AND status = 'pending'"
    ).fetchall()
    assert p[0] != pid and s[0] != sid
    assert _status(ctx, "proposal_approvals", pid) == ("invalidated", "config_snapshot_changed")


# ------------------------------------------------------------------------------ (g) not re-sent
def _invalidate(ctx: AppContext, approval: str, reason: str) -> None:
    with db.transaction(ctx.conn):
        ctx.approvals.invalidate_one(approval, reason, "system")


@pytest.mark.parametrize("case", ["held", "changes_requested", "rejected", "render_failed", "window", "implementing",
                                  "expired"])  # fmt: skip
async def test_not_resent(ctx: AppContext, case: str) -> None:
    publisher = _wire(ctx)
    cid, action, old = await _card(ctx, publisher)
    if case == "held":
        _invalidate(ctx, old, "killed")
        ctx.holds.hold(cid, hold_request(), "uid:2002/sandbox")
    elif case == "rejected":
        ctx.approvals.reject_open(old, tg_user_id=CHAT)
    elif case == "window":
        with db.transaction(ctx.conn):
            ctx.conn.execute("UPDATE approvals SET expires_at = ? WHERE id = ?",
                             (to_iso(ctx.clock.now() + timedelta(seconds=60)), old))  # fmt: skip
        _invalidate(ctx, old, "killed")
    elif case == "implementing":
        _invalidate(ctx, old, "killed")
        with db.transaction(ctx.conn):
            ctx.conn.execute("UPDATE contributions SET state = ? WHERE id = ?", (State.IMPLEMENTING.value, cid))
    elif case == "expired":
        with db.transaction(ctx.conn):
            ctx.conn.execute("UPDATE approvals SET status = 'expired' WHERE id = ?", (old,))
    else:
        _invalidate(ctx, old, case)
    await _tick(ctx, publisher)
    assert _open(ctx, action) == []
    assert _events(ctx, "auto_reissue") == []


# ------------------------------------------------------------------------- (i) a failing re-send
async def test_a_failing_resend_is_audited_once(ctx: AppContext, artifacts: FakeArtifacts) -> None:
    publisher = _wire(ctx)
    _, action, old = await _card(ctx, publisher)
    _invalidate(ctx, old, "killed")

    def broken(cid: str, rev: int, kind: str) -> Any:
        raise ApprovalError("artifacts_unavailable", cid)

    artifacts.hashes = broken  # type: ignore[method-assign]
    for _ in range(3):
        await _tick(ctx, publisher)
    assert _open(ctx, action) == []
    [event] = _events(ctx, "auto_reissue_failed")
    assert event["previous"] == old and event["code"] == "artifacts_unavailable"
