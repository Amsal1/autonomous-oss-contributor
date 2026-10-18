"""Delta 9 (DESIGN §14.18.4.2, AC 47): kill and reload invalidate PR, proposal and skill approvals;
the dry-run toggle reaches only ApprovalService (proposal and skill bindings carry no mode)."""

from __future__ import annotations

import json

import pytest

from ossbot.gatekeeper.context import AppContext
from tests.conftest import packaged_contribution, write_flag
from tests.unit.gatekeeper.decision_helpers import (
    CHAT,
    install_sources,
    proposal_card,
    proposed_contribution,
    skill_card,
    status_of,
)


def _three_armed(ctx: AppContext) -> tuple[str, str, str]:
    install_sources(ctx)
    _, action = packaged_contribution(ctx)
    aid = ctx.create_approval(action, "system")
    ctx.approvals.set_card_message(aid, 555)
    for _ in range(ctx.approvals.part_count(aid)):
        ctx.approvals.mark_part_delivered(aid)
    tok = ctx.approvals.issue_card_tokens(aid)
    ctx.approvals.approve(tok["approve"], tg_user_id=CHAT, chat_id=CHAT, message_id=555)
    pid, ptok = proposal_card(ctx, proposed_contribution(ctx))
    ctx.decisions.proposals.approve(ptok["proposal_start"], tg_user_id=CHAT, chat_id=CHAT, message_id=700)
    sid, stok = skill_card(ctx)
    ctx.decisions.skills.approve(stok["skill_approve"], tg_user_id=CHAT, chat_id=CHAT, message_id=800)
    return aid, pid, sid


def _statuses(ctx: AppContext, aid: str, pid: str, sid: str) -> tuple[str, str, str]:
    return (status_of(ctx, "approvals", aid), status_of(ctx, "proposal_approvals", pid),
            status_of(ctx, "skill_approvals", sid))  # fmt: skip


def test_telegram_or_api_kill_invalidates_all_three(ctx: AppContext) -> None:
    ids = _three_armed(ctx)
    view = ctx.control.kill("test", "tg:4242")
    assert view.state == "killed"
    assert _statuses(ctx, *ids) == ("invalidated",) * 3
    data = json.loads(ctx.conn.execute("SELECT data_json FROM audit_events WHERE event = 'killed'").fetchone()[0])
    assert set(data["invalidated"]) == set(ids)


@pytest.mark.parametrize("path", ["flag_reconcile", "resume_before_reconcile", "resume_log"])
def test_root_kill_paths_invalidate_all_three(ctx: AppContext, path: str) -> None:
    ids = _three_armed(ctx)
    if path == "flag_reconcile":
        write_flag(ctx.paths.killed_flag, {"level": "soft", "reason": "watchdog", "actor": "watchdog", "ts": "x"})
        ctx.control.reconcile()
    elif path == "resume_before_reconcile":
        write_flag(ctx.paths.killed_flag, {"level": "soft", "reason": "watchdog", "actor": "watchdog", "ts": "x"})
        ctx.control.request_resume("tg:4242")
    else:
        ctx.paths.resume_log.parent.mkdir(parents=True, exist_ok=True)
        ctx.paths.resume_log.write_text(json.dumps({"event": "operator_resume_offline", "via": "test"}) + "\n")
        ctx.control.reconcile()
    assert _statuses(ctx, *ids) == ("invalidated",) * 3


def test_confirmed_reload_invalidates_all_three(ctx: AppContext) -> None:
    ids = _three_armed(ctx)
    policy = ctx.paths.config_dir / "policy.yaml"
    policy.write_text(policy.read_text().replace("comments_per_24h: 10", "comments_per_24h: 9"))
    _, invalidated = ctx.snapshots.apply_reload(ctx.snapshots.prepare_reload().hash, "tg:4242")
    assert {i.approval_id for i in invalidated} == set(ids)
    assert _statuses(ctx, *ids) == ("invalidated",) * 3
    proposal = next(i for i in invalidated if i.approval_id == ids[1])
    skill = next(i for i in invalidated if i.approval_id == ids[2])
    assert proposal.action_id is None and proposal.cid is not None
    assert skill.action_id is None and skill.cid is None


def test_startup_snapshot_change_invalidates_all_three(ctx: AppContext) -> None:
    ids = _three_armed(ctx)
    policy = ctx.paths.config_dir / "policy.yaml"
    policy.write_text(policy.read_text().replace("comments_per_24h: 10", "comments_per_24h: 9"))
    ctx.snapshots.load_at_start()
    assert _statuses(ctx, *ids) == ("invalidated",) * 3


def test_reload_reissue_recreates_open_proposal_and_skill_keeping_expiry(ctx: AppContext) -> None:
    _, pid, sid = _three_armed(ctx)
    ctx.decisions.skills.source.staged["git-helper"] = "sr_1"  # type: ignore[attr-defined]
    before = {r[0]: r[1] for r in ctx.conn.execute("SELECT id, expires_at FROM proposal_approvals")}
    ctx.clock.advance(hours=1)  # type: ignore[attr-defined]
    policy = ctx.paths.config_dir / "policy.yaml"
    policy.write_text(policy.read_text().replace("comments_per_24h: 10", "comments_per_24h: 9"))
    _, invalidated = ctx.snapshots.apply_reload(ctx.snapshots.prepare_reload().hash, "tg:4242")
    created = ctx.reissue_cards(invalidated, "tg:4242")
    assert len([c for c in created if c.startswith("a_")]) == 1
    [new_p] = [c for c in created if c.startswith("p_")]
    [new_s] = [c for c in created if c.startswith("s_")]
    after = ctx.conn.execute("SELECT expires_at FROM proposal_approvals WHERE id = ?", (new_p,)).fetchone()[0]
    assert after == before[pid]  # a reload must not extend a proposal window
    assert new_s != sid


def test_dry_run_toggle_reaches_only_pr_approvals(ctx: AppContext) -> None:
    aid, pid, sid = _three_armed(ctx)
    invalidated = ctx.control.set_dry_run(False, "op")
    assert [i.approval_id for i in invalidated] == [aid]
    assert _statuses(ctx, aid, pid, sid) == ("invalidated", "armed", "armed")
