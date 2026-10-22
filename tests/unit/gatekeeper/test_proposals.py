"""Gate 1 (CR-004; DESIGN §9.2, §9.5a, §9.5b, §14.11.15): proposal storage, hash/version binding,
Start work, new versions, declines, Undo, autonomous-only size and filter rules, the expiry sweep."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from ossbot.common.clock import FakeClock
from ossbot.common.schemas import DiscoveredFacts, ProposalSubmit, State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalError, Superseded
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.proposals import ProposalRefused, ProposalService
from ossbot.gatekeeper.state_machine import Actor, TransitionError
from tests.conftest import UID_OPERATOR, UID_WORKER, insert_contribution
from tests.unit.gatekeeper.api_helpers import client
from tests.unit.gatekeeper.decision_helpers import CHAT, status_of

S = State
WORKER = "uid:2001/worker"
OWNER = f"tg:{CHAT}"


def sample(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "problem": "Export throws when a trace has no spans.",
        "approach": ["Guard empty span lists in toOtel()", "Add a regression test next to trace.test.ts"],
        "areas": ["web/src/export/"],
        "kind": "bug",
        "estimate": {"files": 2, "code_lines": 30, "test_lines": 40, "class": "s"},
        "tests": ["web/src/export/trace.test.ts: empty spans"],
        "validation_plan": "vitest on web/src/export",
        "confidence": 0.78,
    }
    doc.update(over)
    return doc


def scouting(ctx: AppContext, source: str = "discover", **cols: Any) -> str:
    return insert_contribution(ctx, S.SCOUTING, source=source, **cols)


def submit(ctx: AppContext, cid: str, revises: int | None = None, code_inspection: dict[str, Any] | None = None,
           **over: Any) -> dict[str, Any]:  # fmt: skip
    body: dict[str, Any] = {"proposal": sample(**over), "revises": revises}
    if code_inspection is not None:
        body["code_inspection"] = code_inspection
    req = ProposalSubmit.model_validate(body)
    return ProposalService(ctx).submit(cid, req, WORKER)


def card(ctx: AppContext, pid: str, message_id: int = 700) -> dict[str, str]:
    ctx.decisions.proposals.set_card_message(pid, message_id)
    return ctx.decisions.proposals.issue_card_tokens(pid)


def start(ctx: AppContext, pid: str, message_id: int = 700) -> dict[str, Any]:
    tokens = card(ctx, pid, message_id)
    tap = {"tg_user_id": CHAT, "chat_id": CHAT, "message_id": message_id}
    arm = ctx.decisions.proposals.approve(tokens["proposal_start"], **tap)
    ctx.decisions.proposals.confirm(arm.confirm_token, **tap)
    return ProposalService(ctx).start(pid, OWNER)


def contribution(ctx: AppContext, cid: str) -> Any:
    row = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
    assert row is not None
    return row


# ------------------------------------------------------------------------------ storage
def test_submit_stores_canonical_version_and_binds_hash(ctx: AppContext) -> None:
    cid = scouting(ctx)
    out = submit(ctx, cid)
    assert out["version"] == 1 and out["est_class"] == "s"
    row = db.one(ctx.conn, "SELECT * FROM proposals WHERE cid = ? AND version = 1", (cid,))
    assert row is not None and json.loads(row["proposal_json"])["estimate"]["class"] == "s"
    assert row["proposal_sha256"] == out["proposal_sha256"]
    c = contribution(ctx, cid)
    assert (c["state"], c["proposal_version"], c["est_class"], c["kind"]) == ("PROPOSED", 1, "s", "bug")
    approval = db.one(ctx.conn, "SELECT * FROM proposal_approvals WHERE id = ?", (out["approval_id"],))
    assert approval is not None and approval["status"] == "pending"
    binding = json.loads(approval["binding_json"])
    assert binding == {"v": 1, "kind": "proposal", "cid": cid, "version": 1, "proposal_sha256": out["proposal_sha256"],
                       "est_class": "s", "user_directed": False, "policy_hash": ctx.snapshots.current.hash}  # fmt: skip


def test_gatekeeper_classifies_and_keeps_the_larger_class(ctx: AppContext) -> None:
    cid = scouting(ctx, source="user")
    out = submit(ctx, cid, estimate={"files": 6, "code_lines": 200, "test_lines": 10, "class": "s"})
    assert out["est_class"] == "m"  # scout said s, the numbers say m


def test_card_renders_escaped_model_text(ctx: AppContext) -> None:
    cid = scouting(ctx)
    out = submit(ctx, cid, problem="<b>bold</b> & co")
    text = ctx.decisions.proposals.card_view(out["approval_id"]).card
    # the kit card (item 46) also defuses the "/" (U8): a word joiner follows it
    assert "&lt;b&gt;bold&lt;/\u2060b&gt; &amp; co" in text and "<b>bold</b>" not in text
    assert "est <b>s</b>" in text and "🤖" in text


# --------------------------------------------------------------------------- Start work
def test_start_work_is_the_only_way_to_fetch(ctx: AppContext) -> None:
    cid = scouting(ctx)
    out = submit(ctx, cid)
    with pytest.raises(TransitionError) as info:  # no fetch/plan/implement before gate 1 (AC 37)
        ctx.state_machine.transition(cid, S.PROPOSED, S.FETCHING, Actor.WORKER)
    assert info.value.code == "no_edge"
    result = start(ctx, out["approval_id"])
    assert result == {"cid": cid, "state": "PROPOSAL_APPROVED", "approved_class": "s"}
    assert status_of(ctx, "proposal_approvals", out["approval_id"]) == "consumed"
    ctx.state_machine.transition(cid, S.PROPOSAL_APPROVED, S.FETCHING, Actor.WORKER)
    assert contribution(ctx, cid)["state"] == "FETCHING"


def test_changed_content_invalidates_the_card(ctx: AppContext) -> None:
    cid = scouting(ctx)
    out = submit(ctx, cid)
    tokens = card(ctx, out["approval_id"])
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE proposals SET proposal_sha256 = ? WHERE cid = ?", ("f" * 64, cid))
    with pytest.raises(Superseded):
        ctx.decisions.proposals.approve(tokens["proposal_start"], tg_user_id=CHAT, chat_id=CHAT, message_id=700)
    assert status_of(ctx, "proposal_approvals", out["approval_id"]) == "invalidated"


def test_new_version_invalidates_and_needs_the_current_version(ctx: AppContext) -> None:
    cid = scouting(ctx)
    v1 = submit(ctx, cid)
    tokens = card(ctx, v1["approval_id"])
    feedback = ProposalService(ctx).request_revision(cid, 1, "adjust", "keep the debug log behind a flag", OWNER)
    assert feedback["kind"] == "adjust" and contribution(ctx, cid)["proposal_feedback_json"]
    with pytest.raises(ProposalRefused) as info:
        submit(ctx, cid, revises=None)
    assert info.value.code == "superseded"
    v2 = submit(ctx, cid, revises=1, approach=["Gate the debug log behind EXPORT_DEBUG"])
    assert v2["version"] == 2 and contribution(ctx, cid)["proposal_feedback_json"] is None
    row = db.one(ctx.conn, "SELECT status, invalidated_reason FROM proposal_approvals WHERE id = ?",
                 (v1["approval_id"],))  # fmt: skip
    assert row is not None and tuple(row) == ("invalidated", "new_proposal_version")
    with pytest.raises(ApprovalError):
        ctx.decisions.proposals.approve(tokens["proposal_start"], tg_user_id=CHAT, chat_id=CHAT, message_id=700)
    assert start(ctx, v2["approval_id"], message_id=701)["state"] == "PROPOSAL_APPROVED"


def test_make_it_smaller_text_and_smallest(ctx: AppContext) -> None:
    cid = scouting(ctx)
    submit(ctx, cid)
    fb = ProposalService(ctx).request_revision(cid, 1, "smaller", "", OWNER)
    assert fb["text"] == "Reduce the scope to fit class xs; keep the fix minimal"
    cid2 = scouting(ctx)
    submit(ctx, cid2, estimate={"files": 1, "code_lines": 3, "test_lines": 5, "class": "xs"})
    with pytest.raises(ProposalRefused) as info:
        ProposalService(ctx).request_revision(cid2, 1, "smaller", "", OWNER)
    assert info.value.code == "already_smallest"


@pytest.mark.parametrize("how", ["paused", "killed"])
def test_start_refused_while_stopped(ctx: AppContext, how: str) -> None:
    cid = scouting(ctx)
    out = submit(ctx, cid)
    tokens = card(ctx, out["approval_id"])
    tap = {"tg_user_id": CHAT, "chat_id": CHAT, "message_id": 700}
    arm = ctx.decisions.proposals.approve(tokens["proposal_start"], **tap)
    ctx.decisions.proposals.confirm(arm.confirm_token, **tap)
    if how == "paused":
        ctx.control.pause("test", "uid:1000/operator")
    else:
        ctx.control.kill("test", "uid:1000/operator")
    with pytest.raises(ApprovalError):
        ProposalService(ctx).start(out["approval_id"], OWNER)
    assert contribution(ctx, cid)["state"] == "PROPOSED"


# ------------------------------------------------------------- autonomous-only rules
def test_autonomous_above_max_class_refused_user_directed_accepted(ctx: AppContext) -> None:
    big = {"files": 12, "code_lines": 400, "test_lines": 300, "class": "l"}
    with pytest.raises(ProposalRefused) as info:
        submit(ctx, scouting(ctx), estimate=big)
    assert (info.value.status, info.value.code) == (422, "above_autonomous_max")
    xxl = {"files": 60, "code_lines": 2500, "test_lines": 2000, "class": "xxl"}
    assert submit(ctx, scouting(ctx, source="user"), estimate=xxl)["est_class"] == "xxl"


@pytest.mark.parametrize("source", ["discover", "user"])
def test_above_the_ceiling_refused_for_everyone(ctx: AppContext, source: str) -> None:
    with pytest.raises(ProposalRefused) as info:
        submit(ctx, scouting(ctx, source=source), estimate={"files": 81, "code_lines": 10, "test_lines": 0,
                                                             "class": "xxl"})  # fmt: skip
    assert info.value.code == "above_ceiling"


def test_filters_apply_to_autonomous_only(ctx: AppContext) -> None:
    with pytest.raises(ProposalRefused) as info:
        submit(ctx, scouting(ctx), kind="new-dependency")
    assert info.value.code == "kind_filtered"
    assert submit(ctx, scouting(ctx, source="user"), kind="new-dependency")["version"] == 1


def test_daily_cap_counts_autonomous_first_versions(ctx: AppContext) -> None:
    for _ in range(5):
        submit(ctx, scouting(ctx))
    with pytest.raises(ProposalRefused) as info:
        submit(ctx, scouting(ctx))
    assert (info.value.status, info.value.code) == (429, "proposal_daily_cap")
    assert submit(ctx, scouting(ctx, source="user"))["version"] == 1  # 👤 items are not counted


def test_only_scouting_or_open_states_accept_proposals(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, S.VETTED)
    with pytest.raises(ProposalRefused) as info:
        submit(ctx, cid)
    assert info.value.code == "not_scouting"


# ----------------------------------------------------------------------- declines + Undo
def test_skip_then_undo_within_10_minutes(ctx: AppContext, clock: FakeClock) -> None:
    cid = scouting(ctx)
    out = submit(ctx, cid)
    tokens = card(ctx, out["approval_id"])
    done = ProposalService(ctx).decline(out["approval_id"], "skipped", OWNER)
    assert done["state"] == "PROPOSAL_REJECTED"
    assert status_of(ctx, "proposal_approvals", out["approval_id"]) == "rejected"
    with pytest.raises(ApprovalError):  # its tokens died with the decision
        ctx.decisions.proposals.approve(tokens["proposal_start"], tg_user_id=CHAT, chat_id=CHAT, message_id=700)
    clock.advance(minutes=9)
    fresh = ProposalService(ctx).undo(cid, out["approval_id"], OWNER)
    assert len(fresh) == 1 and contribution(ctx, cid)["state"] == "PROPOSED"
    assert status_of(ctx, "proposal_approvals", fresh[0]) == "pending"


def test_undo_after_10_minutes_is_too_late(ctx: AppContext, clock: FakeClock) -> None:
    cid = scouting(ctx)
    out = submit(ctx, cid)
    ProposalService(ctx).decline(out["approval_id"], "skipped", OWNER)
    clock.advance(minutes=11)
    with pytest.raises(TransitionError) as info:
        ProposalService(ctx).undo(cid, out["approval_id"], OWNER)
    assert info.value.code == "guard_failed:undo_window"


def test_later_snoozes_with_an_end(ctx: AppContext, clock: FakeClock) -> None:
    cid = scouting(ctx)
    out = submit(ctx, cid)
    with pytest.raises(ProposalRefused):
        ProposalService(ctx).decline(out["approval_id"], "snoozed", OWNER, snooze_days=91)
    done = ProposalService(ctx).decline(out["approval_id"], "snoozed", OWNER, snooze_days=3)
    assert done["state"] == "PROPOSAL_SNOOZED"
    row = contribution(ctx, cid)
    assert row["snooze_until"] == done["snooze_until"] and row["declined_reason"] == "snoozed"


def test_never_repo_and_never_kind_write_through_discovery_prefs(ctx: AppContext) -> None:
    cid = scouting(ctx)
    out = submit(ctx, cid)
    ProposalService(ctx).decline(out["approval_id"], "never_repo", OWNER)
    assert ctx.discovery_prefs.effective("langfuse/langfuse").never_repo is True
    with pytest.raises(ProposalRefused) as info:
        submit(ctx, scouting(ctx))
    assert info.value.code == "never_repo"
    other = scouting(ctx, upstream="tobenwarrior/hermes-kiro-provider")
    out2 = submit(ctx, other, kind="docs")
    ProposalService(ctx).decline(out2["approval_id"], "never_kind", OWNER)
    assert "docs" in ctx.discovery_prefs.effective().filters.excluded_kinds
    events = [r[0] for r in ctx.conn.execute("SELECT event FROM audit_events WHERE event LIKE 'discovery_prefs%'")]
    assert events == ["discovery_prefs_changed", "discovery_prefs_changed"]


def _discovered_candidate(ctx: AppContext, repo: str = "openfaas/faas") -> None:
    ctx.discovered.record(
        DiscoveredFacts.model_validate({"repo": repo, "source": "search", "status": "candidate", "stars": 25000}),
        "uid:2001/worker",
    )


def test_never_on_a_discovered_repo_writes_its_row(ctx: AppContext) -> None:
    """Design §9.4: Never on a non-config repo goes to discovered_targets, not discovery_prefs."""
    _discovered_candidate(ctx)
    cid = scouting(ctx, upstream="openfaas/faas", category="open-discovery")
    out = submit(ctx, cid)
    ProposalService(ctx).decline(out["approval_id"], "never_repo", OWNER)
    found = ctx.discovered.get("openfaas/faas")
    assert found is not None and found.status == "never" and found.decided_by == OWNER
    assert ctx.discovery_prefs.rows() == []
    assert contribution(ctx, cid)["state"] == "PROPOSAL_REJECTED"


def test_submit_refused_for_a_discovered_never_repo(ctx: AppContext) -> None:
    _discovered_candidate(ctx)
    cid = scouting(ctx, upstream="openfaas/faas", category="open-discovery")
    ctx.discovered.decide("openfaas/faas", "never", OWNER)
    with pytest.raises(ProposalRefused) as info:
        submit(ctx, cid)
    assert (info.value.status, info.value.code) == (422, "never_repo")


def test_decline_of_an_armed_card_refused(ctx: AppContext) -> None:
    cid = scouting(ctx)
    out = submit(ctx, cid)
    tokens = card(ctx, out["approval_id"])
    ctx.decisions.proposals.approve(tokens["proposal_start"], tg_user_id=CHAT, chat_id=CHAT, message_id=700)
    with pytest.raises(ProposalRefused) as info:
        ProposalService(ctx).decline(out["approval_id"], "skipped", OWNER)
    assert info.value.code == "cancel_confirm_first"


# ------------------------------------------------------------------------------- expiry
def test_expired_proposal_moves_to_proposal_expired(ctx: AppContext, clock: FakeClock) -> None:
    cid = scouting(ctx)
    submit(ctx, cid)
    assert ProposalService(ctx).sweep_expired() == []
    clock.advance(hours=73)
    ctx.decisions.proposals.expire_due()
    assert ProposalService(ctx).sweep_expired() == [cid]
    assert contribution(ctx, cid)["state"] == "PROPOSAL_EXPIRED"
    ctx.state_machine.transition(cid, S.PROPOSAL_EXPIRED, S.SCOUTING, Actor.WORKER)  # re-scouted


# ---------------------------------------------------------------------------------- API
async def test_api_roles_and_validation(ctx: AppContext) -> None:
    cid = scouting(ctx)
    async with client(ctx, UID_OPERATOR) as c:
        assert (await c.post(f"/v1/contributions/{cid}/proposal", json={"proposal": sample()})).status_code == 403
    async with client(ctx, UID_WORKER) as c:
        bad = sample(tests=[])
        assert (await c.post(f"/v1/contributions/{cid}/proposal", json={"proposal": bad})).status_code == 400
        extra = {**sample(), "shell": "rm -rf /"}
        assert (await c.post(f"/v1/contributions/{cid}/proposal", json={"proposal": extra})).status_code == 400
        ok = await c.post(f"/v1/contributions/{cid}/proposal", json={"proposal": sample()})
        assert ok.status_code == 201 and ok.json()["version"] == 1
        got = await c.get(f"/v1/contributions/{cid}/proposal")
        assert got.status_code == 200 and got.json()["proposal"]["kind"] == "bug"
        big = sample(estimate={"files": 12, "code_lines": 400, "test_lines": 1, "class": "l"})
        cid2 = scouting(ctx)
        resp = await c.post(f"/v1/contributions/{cid2}/proposal", json={"proposal": big})
        assert resp.status_code == 422 and resp.json()["error"] == "above_autonomous_max"


def test_no_suite_reason_replaces_tests() -> None:
    ProposalSubmit.model_validate({"proposal": sample(tests=[], no_suite_reason="docs-only change")})
    with pytest.raises(ValueError, match="no_suite_reason"):
        ProposalSubmit.model_validate({"proposal": sample(tests=[])})


def test_on_main_verdict_needs_evidence_and_code_inspection_is_not_model_output() -> None:
    """§9.5a: ``on_main`` defaults to needs_change; a skip verdict cites evidence; the runner-measured
    ``code_inspection`` lives on ``ProposalSubmit`` only (``extra=forbid`` keeps it out of model output)."""
    assert ProposalSubmit.model_validate({"proposal": sample()}).proposal.on_main == "needs_change"
    with pytest.raises(ValueError, match="on_main_evidence"):
        ProposalSubmit.model_validate({"proposal": sample(on_main="already_fixed")})
    with pytest.raises(ValueError):
        ProposalSubmit.model_validate({"proposal": sample(on_main="maybe", on_main_evidence=["x"])})
    with pytest.raises(ValueError):
        ProposalSubmit.model_validate({"proposal": sample(on_main_evidence=["x" * 301])})
    with pytest.raises(ValueError):
        ProposalSubmit.model_validate({"proposal": sample(on_main_evidence=["x"] * 6)})
    seen = {"repo_reads": 0, "offloaded": 0, "self_reported_unread": False}
    with pytest.raises(ValueError, match="code_inspection"):
        ProposalSubmit.model_validate({"proposal": sample(code_inspection=seen)})
    ok = ProposalSubmit.model_validate({"proposal": sample(), "code_inspection": seen})
    assert ok.code_inspection is not None and ok.code_inspection.repo_reads == 0
    for bad in ({**seen, "repo_reads": -1}, {**seen, "offloaded": 100_001}, {**seen, "extra": 1},
                {"repo_reads": 1, "offloaded": 0}):  # fmt: skip
        with pytest.raises(ValueError):
            ProposalSubmit.model_validate({"proposal": sample(), "code_inspection": bad})


def test_code_inspection_is_stored_inside_the_bound_doc(ctx: AppContext) -> None:
    seen = {"repo_reads": 7, "offloaded": 1, "self_reported_unread": False}
    one, two, legacy = scouting(ctx), scouting(ctx), scouting(ctx)
    a = submit(ctx, one, code_inspection=seen)
    b = submit(ctx, two, code_inspection={**seen, "repo_reads": 0})
    c = submit(ctx, legacy)
    doc = json.loads(db.one(ctx.conn, "SELECT proposal_json FROM proposals WHERE cid = ?", (one,))[0])  # type: ignore[index]
    assert doc["code_inspection"] == seen and doc["on_main"] == "needs_change"
    assert len({a["proposal_sha256"], b["proposal_sha256"], c["proposal_sha256"]}) == 3  # hash-bound
    old = json.loads(db.one(ctx.conn, "SELECT proposal_json FROM proposals WHERE cid = ?", (legacy,))[0])  # type: ignore[index]
    assert "code_inspection" not in old


@pytest.mark.parametrize("verdict", ["already_fixed", "not_reproducible"])
def test_a_skip_verdict_is_not_a_proposal(ctx: AppContext, verdict: str) -> None:
    cid = scouting(ctx)
    with pytest.raises(ProposalRefused) as info:
        submit(ctx, cid, on_main=verdict, on_main_evidence=["src/a.go:10"])
    assert (info.value.status, info.value.code) == (422, "not_a_proposal")
    assert db.one(ctx.conn, "SELECT 1 FROM proposals WHERE cid = ?", (cid,)) is None
    assert db.one(ctx.conn, "SELECT 1 FROM proposal_approvals WHERE cid = ?", (cid,)) is None
    assert contribution(ctx, cid)["state"] == "SCOUTING"


def test_sweep_ignores_live_cards(ctx: AppContext, clock: FakeClock) -> None:
    cid = scouting(ctx)
    submit(ctx, cid)
    clock.advance(hours=71)
    ctx.decisions.proposals.expire_due()
    assert ProposalService(ctx).sweep_expired() == []
    assert timedelta(hours=71) < timedelta(hours=72)
