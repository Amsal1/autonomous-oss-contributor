"""The approval queue (DESIGN §14.11.13; AC 11, AC 43): deterministic class ordering, gate 2 before
gate 1, proposals promoted within 6 h of expiry, and ``📥 Open n`` re-sending with the expiry kept."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from ossbot.common.clock import to_iso
from ossbot.common.ids import new_id
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.review import queue
from tests.conftest import consumed_proposal, insert_action, insert_contribution, packaged_contribution
from tests.fakes.holds import hold_request
from tests.telegram.reviewkit import labels, newest_approval, publish, sent_with, status, text, toast, token
from tests.telegram.tgharness import TgRig, make_rig
from tests.unit.gatekeeper.decision_helpers import install_sources, proposal_card, proposed_contribution, skill_card


def _approve(ctx: AppContext, action: str, *, hours: int = 72) -> str:
    with db.transaction(ctx.conn):
        approval = ctx.create_approval(action, "test")
        ctx.conn.execute("UPDATE approvals SET expires_at = ? WHERE id = ?",
                         (to_iso(ctx.clock.now() + timedelta(hours=hours)), approval))  # fmt: skip
    return approval


def _proposal(ctx: AppContext, *, hours: int, source: str = "discover", score: float = 0.5) -> str:
    cid = insert_contribution(ctx, State.PROPOSED, source=source, est_class="s", score=score)
    now = ctx.clock.now()
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO proposal_approvals (id, cid, version, binding_json, binding_hash, status, tg_chat_id,"
            " created_at, expires_at) VALUES (?,?,1,'{}','h','pending',?,?,?)",
            (new_id("p"), cid, ctx.telegram_chat_id, to_iso(now), to_iso(now + timedelta(hours=hours))),
        )
    return cid


def test_class_order_and_gates(ctx: AppContext) -> None:
    pr_cid, pr = packaged_contribution(ctx)
    _approve(ctx, pr, hours=10)  # class 3 (open_pr) even though it expires first
    push_cid, push = packaged_contribution(ctx, pr_number=7)
    _approve(ctx, push, hours=50)  # class 2
    ours = insert_action(ctx, push_cid, "comment", {"target_number": 7, "body": "On our PR"})
    _approve(ctx, ours, hours=70)  # class 0: someone is waiting
    other = insert_action(ctx, pr_cid, "comment", {"target_number": 99, "body": "Elsewhere"})
    _approve(ctx, other, hours=1)  # class 5
    user_cid, user_action = packaged_contribution(ctx)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE contributions SET source = 'user' WHERE id = ?", (user_cid,))
    _approve(ctx, user_action, hours=60)  # class 1 (👤)
    late = _proposal(ctx, hours=3)  # promoted to class 2 within 6 h
    early = _proposal(ctx, hours=40, score=0.9)  # class 4
    order = [(i.gate, i.class_rank, i.subject) for i in queue.ordered(ctx)]
    assert order == [(2, 0, push_cid), (2, 1, user_cid), (2, 2, push_cid), (2, 3, pr_cid), (2, 5, pr_cid),
                     (1, 2, late), (1, 4, early)]  # fmt: skip
    oldest = [(i.gate, i.subject) for i in queue.ordered(ctx, "oldest")]
    assert oldest[0] == (2, pr_cid) and oldest[-1] == (1, early)


def test_maintainer_fix_expired_items_and_size_cards(ctx: AppContext) -> None:
    """F009-R1-8: a maintainer-triggered push_update is class 0; an approval past its expiry is no open
    card even before the sweep, but its still-eligible item is listed as ``⌛ expired`` (post-v1 OD-C2);
    ceiling-hit (class 2) and RESCOPE_REQUIRED (class 3) items are listed."""
    fixed_cid, fixed = packaged_contribution(ctx, pr_number=8)
    with db.transaction(ctx.conn):
        ctx.audit.append("uid:2001/worker", "transition", {"from": "WATCHING", "to": "IMPLEMENTING",
                                                           "evidence": {"reason": "fix_round"}}, fixed_cid)  # fmt: skip
    _approve(ctx, fixed, hours=60)
    gone_cid, gone = packaged_contribution(ctx)
    _approve(ctx, gone, hours=-1)  # past expires_at, still 'pending' until the sweep
    ceiling = insert_contribution(ctx, State.RESCOPE_REQUIRED, size_report_json='{"rescope_reason": "ceiling"}')
    overrun = insert_contribution(ctx, State.RESCOPE_REQUIRED, size_report_json='{"rescope_reason": "overrun"}')
    items = queue.ordered(ctx)
    order = [(i.class_rank, i.subject, i.family) for i in items]
    assert order == [(0, fixed_cid, "a"), (2, ceiling, "r"), (3, gone_cid, "a"), (3, overrun, "r")]
    assert items[2].label.endswith("· ⌛ expired")


def _transition(ctx: AppContext, cid: str, from_: str, to: str, reason: str | None) -> None:
    evidence = {"reason": reason} if reason else {}
    with db.transaction(ctx.conn):
        ctx.audit.append("x", "transition", {"from": from_, "to": to, "evidence": evidence}, cid)


def test_owner_fix_round_is_labelled_and_ranked_separately(ctx: AppContext) -> None:
    """ADR-0003 addendum (2026-10-23): the owner's /fix round is class 1 ``🔧 PR #n your fix``; a maintainer
    round stays class 0 after an in-round VALIDATION_FAILED -> IMPLEMENTING retry (the round boundary is
    the newest WATCHING -> IMPLEMENTING)."""
    owner_cid, owner = packaged_contribution(ctx, pr_number=8)
    _transition(ctx, owner_cid, "WATCHING", "IMPLEMENTING", "owner_fix_round")
    _approve(ctx, owner, hours=60)
    maint_cid, maint = packaged_contribution(ctx, pr_number=9)
    _transition(ctx, maint_cid, "WATCHING", "IMPLEMENTING", "fix_round")
    _transition(ctx, maint_cid, "VALIDATION_FAILED", "IMPLEMENTING", None)
    _approve(ctx, maint, hours=60)
    plain_cid, plain = packaged_contribution(ctx, pr_number=10)  # a gate-2 Request changes round only
    _transition(ctx, plain_cid, "CHANGES_REQUESTED", "IMPLEMENTING", None)
    _approve(ctx, plain, hours=60)
    items = {i.subject: i for i in queue.ordered(ctx)}
    assert (items[owner_cid].class_rank, items[maint_cid].class_rank, items[plain_cid].class_rank) == (1, 0, 2)
    assert items[owner_cid].label.startswith("🔧 PR #8 your fix · ")
    assert items[maint_cid].label.startswith("🔁 PR #9 fix · ")
    assert items[plain_cid].label.startswith("🔁 PR #10 fix · ")
    assert queue.fix_trigger(ctx, owner_cid) == "owner" and queue.maintainer_triggered(ctx, maint_cid)
    assert queue.fix_trigger(ctx, plain_cid) is None


def test_autonomous_proposals_rank_by_score_not_target_priority() -> None:
    when = datetime(2026, 10, 19, tzinfo=UTC)
    a = queue.Item(1, 4, when, 9, "t", "p", "c_A", "a", score=0.2)
    b = queue.Item(1, 4, when, 0, "t", "p", "c_B", "b", score=0.8)
    assert sorted([a, b], key=lambda i: i.key("priority")) == [b, a]
    c = queue.Item(2, 3, when, 0, "t", "a", "c_C", "c")
    d = queue.Item(2, 3, when, 5, "t", "a", "c_D", "d")
    assert sorted([c, d], key=lambda i: i.key("priority")) == [d, c]  # elsewhere the target priority wins


def test_empty_queue_and_paging(ctx: AppContext) -> None:
    assert "Nothing is waiting for you." in str(queue.view(ctx).blocks)
    for _ in range(7):
        _, action = packaged_contribution(ctx)
        _approve(ctx, action)
    view = queue.view(ctx, page=2)
    assert view.state == {"page": 2, "sort": "priority"}
    assert [b.label for b in view.rows[0]] == ["📥 Open 6", "📥 Open 7"]


def test_waiting_items_without_a_live_card(ctx: AppContext) -> None:
    """Post-v1 OD-C2: what waits on the owner is listed whether or not a live card exists."""
    _, skills = install_sources(ctx)
    skills.staged["git-helper"] = "sr_1"
    gone_cid, gone = packaged_contribution(ctx)  # the owner's case: AWAITING_APPROVAL, card invalidated
    with db.transaction(ctx.conn):
        ctx.approvals.invalidate_one(_approve(ctx, gone), "config_snapshot_changed", "system")
    prop_cid = proposed_contribution(ctx)
    pid, _ = proposal_card(ctx, prop_cid)
    sid, _ = skill_card(ctx)
    with db.transaction(ctx.conn):
        ctx.decisions.proposals.invalidate_all_open("config_snapshot_changed")
        ctx.decisions.skills.invalidate_all_open("tier_changed")
    held_cid, _ = packaged_contribution(ctx)
    ctx.holds.hold(held_cid, hold_request(), "uid:2002/sandbox")
    failed_cid = insert_contribution(ctx, State.SUBMIT_FAILED)
    failed = insert_action(ctx, failed_cid, "open_pr", {"rev": 1}, status="failed", error_class="github_5xx")
    implementing, stuck = packaged_contribution(ctx)
    with db.transaction(ctx.conn):  # not waiting on the owner: a revision is being built
        ctx.conn.execute("UPDATE contributions SET state = 'IMPLEMENTING' WHERE id = ?", (implementing,))
    got = {(i.family, i.subject): i.label for i in queue.items(ctx)}
    assert set(got) == {("a", gone_cid), ("p", prop_cid), ("s", "git-helper"), ("h", held_cid), ("f", failed)}
    assert got[("a", gone_cid)].endswith("· no live card")
    assert got[("p", prop_cid)].endswith("· no live card") and got[("s", "git-helper")].endswith("· no live card")
    assert got[("h", held_cid)] == "⏸ Held · langfuse/langfuse · egress anomaly"
    assert got[("f", failed)] == "⚠️ Submit failed · langfuse/langfuse · github_5xx"
    assert stuck and pid and sid
    ranks = {i.family: i.class_rank for i in queue.items(ctx)}
    assert ranks["h"] == 0 and ranks["f"] == 1


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


async def test_queue_command_and_open_keep_the_expiry(rig: TgRig) -> None:
    ctx = rig.ctx
    cid, action = packaged_contribution(ctx)
    card = await publish(rig, action)
    before = db.one(ctx.conn, "SELECT expires_at FROM approvals WHERE id = ?", (card.approval,))[0]
    ctx.clock.advance(hours=2)  # type: ignore[attr-defined]
    await rig.say("/queue")
    mid = sent_with(rig, "Queue")
    assert "📥 <b>Queue</b> · 1 waiting" in text(rig, mid)
    assert labels(rig, mid) == ["📥 Open 1", "⬆️ Oldest first", "✅ 🎯 By priority"]
    await rig.tap(token(rig, mid, "📥 Open 1"), message=mid)
    assert toast(rig) == "Re-sent the card"
    new = newest_approval(ctx, action)
    assert new != card.approval
    assert db.one(ctx.conn, "SELECT expires_at FROM approvals WHERE id = ?", (new,))[0] == before
    await rig.tap(token(rig, mid, "⬆️ Oldest"), message=mid)
    assert labels(rig, mid)[-2:] == ["✅ ⬆️ Oldest first", "🎯 By priority"]
    assert cid in str(ctx.conn.execute("SELECT cid FROM approvals a JOIN outbound_actions o ON o.id = a.action_id"
                                       " WHERE a.id = ?", (new,)).fetchone()[0])  # fmt: skip


async def _open_first(rig: TgRig) -> str:
    await rig.say("/queue")
    mid = sent_with(rig, "Queue")
    await rig.tap(token(rig, mid, "📥 Open 1"), message=mid)
    return toast(rig)


def _open_cards(ctx: AppContext, action: str) -> list[str]:
    return [str(r[0]) for r in ctx.conn.execute("SELECT id FROM approvals WHERE action_id = ? AND status IN"
                                                " ('pending','armed')", (action,))]  # fmt: skip


async def test_open_awaiting_approval_without_a_card(rig: TgRig) -> None:
    """The owner's case: /queue showed 0 items while /approve <cid> worked."""
    ctx = rig.ctx
    cid, action = packaged_contribution(ctx)
    card = await publish(rig, action)
    with db.transaction(ctx.conn):
        ctx.approvals.invalidate_one(card.approval, "config_snapshot_changed", "system")
    await rig.say("/queue")
    mid = sent_with(rig, "Queue")
    assert "📥 <b>Queue</b> · 1 waiting" in text(rig, mid) and "no live card" in text(rig, mid)
    await rig.tap(token(rig, mid, "📥 Open 1"), message=mid)
    assert toast(rig) == "Re-sent the card"
    assert len(_open_cards(ctx, action)) == 1 and status(ctx, card.approval) == "invalidated"
    assert cid


async def test_open_expired_restarts_the_window(rig: TgRig) -> None:
    ctx = rig.ctx
    _, action = packaged_contribution(ctx)
    card = await publish(rig, action)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE approvals SET status = 'expired' WHERE id = ?", (card.approval,))
    ctx.clock.advance(hours=73)  # type: ignore[attr-defined]
    assert await _open_first(rig) == "Re-sent the card"
    [fresh] = _open_cards(ctx, action)
    expires = db.one(ctx.conn, "SELECT expires_at FROM approvals WHERE id = ?", (fresh,))[0]
    assert expires > to_iso(ctx.clock.now() + timedelta(hours=71))


async def test_open_proposal_and_skill_without_a_card(rig: TgRig) -> None:
    ctx = rig.ctx
    _, skills = install_sources(ctx)
    skills.staged["git-helper"] = "sr_1"
    cid = proposed_contribution(ctx)
    proposal_card(ctx, cid)
    with db.transaction(ctx.conn):
        ctx.decisions.proposals.invalidate_all_open("config_snapshot_changed")
    assert await _open_first(rig) == "Re-sent the proposal card"
    assert db.one(ctx.conn, "SELECT 1 FROM proposal_approvals WHERE cid = ? AND status = 'pending'", (cid,))
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE contributions SET state = 'PLANNED' WHERE id = ?", (cid,))
        ctx.decisions.proposals.invalidate_all_open("config_snapshot_changed")
    skill_card(ctx)
    with db.transaction(ctx.conn):
        ctx.decisions.skills.invalidate_all_open("tier_changed")
    assert await _open_first(rig) == "Re-sent the skill card"
    assert db.one(ctx.conn, "SELECT 1 FROM skill_approvals WHERE skill = 'git-helper' AND status = 'pending'")


async def test_open_held_resends_the_hold_card(rig: TgRig) -> None:
    ctx = rig.ctx
    cid, _ = packaged_contribution(ctx)
    ctx.holds.hold(cid, hold_request(), "uid:2002/sandbox")
    await rig.flush()
    cards = len([p for p in rig.sent() if "Egress anomaly" in str(p.get("text", ""))])
    assert await _open_first(rig) == "Re-sent the hold card"
    assert len([p for p in rig.sent() if "Egress anomaly" in str(p.get("text", ""))]) == cards + 1


async def test_open_submit_failed_is_the_retry(rig: TgRig) -> None:
    ctx = rig.ctx
    cid, action = packaged_contribution(ctx)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE contributions SET state = 'SUBMIT_FAILED' WHERE id = ?", (cid,))
        ctx.conn.execute("UPDATE outbound_actions SET status = 'failed', error_class = 'github_5xx' WHERE id = ?",
                         (action,))  # fmt: skip
    assert await _open_first(rig) == "🔁 A fresh card follows (approve and confirm again)."
    row = db.one(ctx.conn, "SELECT o.id, c.state FROM outbound_actions o JOIN contributions c ON c.id = o.cid"
                 " WHERE o.cid = ? AND o.status = 'pending_approval'", (cid,))  # fmt: skip
    assert row is not None and row["state"] == "AWAITING_APPROVAL" and len(_open_cards(ctx, str(row["id"]))) == 1
    retried = ctx.conn.execute("SELECT data_json FROM audit_events WHERE event = 'retry_requested'").fetchall()
    assert len(retried) == 1


async def test_an_item_no_longer_eligible_answers_why(rig: TgRig) -> None:
    ctx = rig.ctx
    cid, action = packaged_contribution(ctx)
    card = await publish(rig, action)
    with db.transaction(ctx.conn):
        ctx.approvals.invalidate_one(card.approval, "config_snapshot_changed", "system")
    await rig.say("/queue")
    mid = sent_with(rig, "Queue")
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE contributions SET state = 'IMPLEMENTING' WHERE id = ?", (cid,))
    before = int(ctx.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0])
    await rig.tap(token(rig, mid, "📥 Open 1"), message=mid)
    assert toast(rig) == "Nothing to re-send (state IMPLEMENTING)"
    assert int(ctx.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]) == before


# ------------------------------------------------------------- reruns with network (ADR-0024, family n)
def _switch(ctx: AppContext, mode: str, setting: str) -> None:
    default = ctx.config.egress.mode_default
    if ctx.control.egress_mode(default) != mode:
        ctx.control.set_egress_mode(mode, "tg:4242", default, egress=ctx.config.egress)  # type: ignore[arg-type]
    ctx.control.set_implement_network(setting, "tg:4242", default)  # type: ignore[arg-type]


def _failed(ctx: AppContext, **cols: object) -> str:
    cols.setdefault("state_reason", "needs_network")
    cid = insert_contribution(ctx, State.FAILED, upstream="runatlantis/atlantis", proposal_version=1, **cols)
    consumed_proposal(ctx, cid)
    return cid


@pytest.mark.parametrize(("mode", "setting", "suffix"), [
    ("sandbox_open", "on_request", ""),
    ("open", "auto", " · needs sandbox_open"),
    ("sandbox_open", "off", " · implement network off"),
])  # fmt: skip
def test_rerun_family_listed_after_gate_1(ctx: AppContext, mode: str, setting: str, suffix: str) -> None:
    _switch(ctx, mode, setting)
    cid = _failed(ctx)
    _proposal(ctx, hours=24)
    items = queue.ordered(ctx)
    assert [i.family for i in items] == ["p", "n"]
    rerun = items[-1]
    assert (rerun.gate, rerun.class_rank, rerun.subject, rerun.shown_cid) == (0, 4, cid, cid)
    assert rerun.label == f"🌐 Rerun? · runatlantis/atlantis · needs_network{suffix}"
    shown = str(queue.view(ctx).blocks)
    assert "Gate 1 · proposals (1)" in shown and "Reruns with network (1)" in shown


@pytest.mark.parametrize("case", ["strict", "bound_used", "window_passed", "not_eligible", "pr"])
def test_rerun_family_not_listed(ctx: AppContext, case: str) -> None:
    _switch(ctx, "sandbox_open", "on_request")
    if case == "strict":
        ctx.conn.execute("INSERT INTO discovered_targets (repo, source, status, first_seen, updated_at,"
                         " egress_strict) VALUES ('acme/strict-tool', 'owner', 'kept', 't', 't', 1)")  # fmt: skip
        cid = insert_contribution(ctx, State.FAILED, upstream="acme/strict-tool", proposal_version=1,
                                  state_reason="needs_network")  # fmt: skip
        consumed_proposal(ctx, cid)
    elif case == "bound_used":
        _failed(ctx, network_reruns=1)
    elif case == "window_passed":
        _failed(ctx, updated_at=to_iso(ctx.clock.now() - timedelta(days=8)))
    elif case == "not_eligible":
        _failed(ctx, state_reason="output_invalid")
    else:
        _failed(ctx, pr_number=7)
    assert [i for i in queue.items(ctx) if i.family == "n"] == []


async def test_open_rerun_runs_the_same_action(rig: TgRig) -> None:
    ctx = rig.ctx
    _switch(ctx, "sandbox_open", "on_request")
    cid = _failed(ctx)
    await rig.say("/queue")
    mid = sent_with(rig, "Queue")
    await rig.tap(token(rig, mid, "📥 Open 1"), message=mid)
    await rig.flush()
    confirm = sent_with(rig, "Rerun with network?")
    assert labels(rig, confirm) == ["🌐 Rerun now", "↩️ Cancel"]
    await rig.tap(token(rig, confirm, "🌐 Rerun now"), message=confirm)
    assert db.one(ctx.conn, "SELECT state FROM contributions WHERE id = ?", (cid,))[0] == "PROPOSAL_APPROVED"
