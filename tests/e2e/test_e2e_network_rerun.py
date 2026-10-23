"""🌐 Rerun with network end to end (ADR-0024, design §4d/§5 R2.4/R2.8): egress mode ``sandbox_open`` and
``implement_network: on_request`` (this test database only). The item fails ``needs_network`` after its one
re-fetch, the terminal FYI offers the button, retention prunes while it is FAILED, the owner's confirm tap
(``network_rerun.start_rerun``) drives the H-only edge, and the rerun re-fetches, reuses the plan (no planner
job), runs the coder with agent-exec on its ``-exec`` network (the gatekeeper's audited decision ``web/rerun``),
validates, reviews and packages into a gate-2 ``open_pr`` card with the provenance line. Nothing is pushed and
dry-run stays on. A second failure gets no button (bound 1); outside ``sandbox_open`` the tap is refused; and
a rerun that rescopes loses its grant, so the new gate 1's coder runs offline (carried criterion C-D17)."""

from __future__ import annotations

import json

import pytest

from ossbot.gatekeeper import db, network_rerun, packaging
from ossbot.gatekeeper.proposals import ProposalService
from ossbot.gatekeeper.telegram import cards
from ossbot.gatekeeper.telegram.cards import view_for_action
from ossbot.gatekeeper.telegram.commands.network_rerun import refusal_text
from tests.e2e.conftest import CODER, E2E, EDITS, PROPOSAL
from tests.unit.gatekeeper.test_proposals import OWNER
from tests.unit.gatekeeper.test_size_report import patch_policy

NEEDS = {**CODER, "needs_network": ["com.google.guava:guava:33.3.1-jre"]}
BUTTON = "🌐 Rerun with network"
NET_LINE = "🌐 The coder had network in this round (rerun)"


def _switches(e2e: E2E, mode: str = "sandbox_open", setting: str = "on_request") -> None:
    ctx = e2e.ctx
    default = ctx.config.egress.mode_default
    if ctx.control.egress_mode(default) != mode:
        ctx.control.set_egress_mode(mode, "tg:4242", default, egress=ctx.config.egress)  # type: ignore[arg-type]
    ctx.control.set_implement_network(setting, "tg:4242", default)  # type: ignore[arg-type]


def _fyi(e2e: E2E, key: str) -> str:
    row = db.one(e2e.ctx.conn, "SELECT payload_json FROM tg_outbox WHERE dedupe_key = ?", (key,))
    assert row is not None, key
    return json.dumps(json.loads(row[0]), ensure_ascii=False)


def _fail_needs_network(e2e: E2E) -> str:
    """discover -> gate 1 -> fetch, plan, coder (needs_network), the one re-fetch, coder again -> FAILED."""
    e2e.refetch_deps = ["build.gradle.kts"]
    cid = e2e.discover_one()
    e2e.start_work(cid)
    assert e2e.run_until(cid, "FAILED") == "FAILED", e2e.why(cid)
    row = db.one(e2e.ctx.conn, "SELECT state_reason FROM contributions WHERE id = ?", (cid,))
    assert row[0] == "needs_network"
    assert e2e.jobs_of("fetch") == ["fetch:scout", "fetch:build", "fetch:refetch"]
    assert e2e.exec_networks == ["none", "none"]  # on_request without a grant: offline
    return cid


def _decisions(e2e: E2E, cid: str) -> list[tuple[str, str]]:
    rows = e2e.ctx.conn.execute("SELECT data_json FROM audit_events WHERE cid = ? AND event ="
                                " 'implement_egress_decided' ORDER BY seq", (cid,))  # fmt: skip
    return [(d["egress"], d["reason"]) for d in (json.loads(r[0]) for r in rows)]


def test_rerun_with_network_reaches_gate_2_with_nothing_pushed(e2e: E2E) -> None:
    _switches(e2e)
    e2e.answer("coder", NEEDS, NEEDS, CODER)
    cid = _fail_needs_network(e2e)
    assert BUTTON in _fyi(e2e, f"terminal:{cid}")
    assert packaging.prune_retention(e2e.ctx) is not None  # the FAILED item's rev mirrors may go
    e2e.ready_scheduler().tick()  # the sweep may destroy the task volume while it is FAILED
    assert network_rerun.refusal(e2e.ctx, cid) is None  # what the button checks before the confirm card
    before = len(e2e.pipeline_log())
    done = network_rerun.start_rerun(e2e.ctx, cid, "tg:4242")  # what 🌐 Rerun now calls
    assert (done.n, done.max) == (1, 1) and e2e.state(cid) == "PROPOSAL_APPROVED"
    assert e2e.run_until(cid, "AWAITING_APPROVAL") == "AWAITING_APPROVAL", e2e.why(cid)
    # the plan is reused: no planner job
    assert e2e.pipeline_log()[before:] == ["fetch:build", "kiro:coder", "validate", "kiro:reviewer",
                                           "kiro:summarizer", "package"]  # fmt: skip
    assert e2e.exec_networks[-1].startswith("ossbot-t-") and e2e.exec_networks[-1].endswith("-exec")
    assert _decisions(e2e, cid)[-1] == ("web", "rerun")
    job = e2e.jobs.store.conn.execute("SELECT exec_network FROM jobs WHERE cid = ? AND profile = 'coder'"
                                      " ORDER BY rowid DESC LIMIT 1", (cid,)).fetchone()  # fmt: skip
    assert job[0] == "web"
    planned = db.one(e2e.ctx.conn, "SELECT data_json FROM audit_events WHERE cid = ? AND event = 'transition' AND"
                     " json_extract(data_json, '$.to') = 'PLANNED' ORDER BY seq DESC LIMIT 1", (cid,))  # fmt: skip
    assert json.loads(planned[0])["evidence"]["reused"] is True  # no planner job: the stored plan
    approval = e2e.pending_approval(cid)
    action = db.one(e2e.ctx.conn, "SELECT action_id FROM approvals WHERE id = ?", (approval,))[0]
    card = view_for_action(e2e.ctx.conn, str(action), True)
    assert card.kind == "open_pr" and card.network_reasons == ("rerun",)
    assert NET_LINE in cards.render(card).card  # the gate-2 card says the coder had network
    row = db.one(e2e.ctx.conn, "SELECT network_grant, network_grant_version, network_reruns FROM contributions"
                 " WHERE id = ?", (cid,))  # fmt: skip
    assert tuple(row) == (0, None, 1)  # cleared on the way into AWAITING_APPROVAL
    assert e2e.writes() == [] and e2e.pusher.pushes == []
    assert db.current_dry_run(e2e.ctx.conn)


def test_a_second_failure_gets_no_button(e2e: E2E) -> None:
    _switches(e2e)
    e2e.answer("coder", NEEDS)  # every coder run asks for the network-only dependency
    cid = _fail_needs_network(e2e)
    network_rerun.start_rerun(e2e.ctx, cid, "tg:4242")
    assert e2e.run_until(cid, "FAILED") == "FAILED", e2e.why(cid)
    second = _fyi(e2e, f"terminal:{cid}:1")  # once per run: the rerun's end is reported too
    assert BUTTON not in second and "the sandbox could not build or test this repo offline" in second
    assert network_rerun.refusal(e2e.ctx, cid) == "network_reruns"
    assert e2e.writes() == []


def test_outside_sandbox_open_the_tap_is_refused(e2e: E2E) -> None:
    _switches(e2e, mode="open")
    e2e.answer("coder", NEEDS)
    cid = _fail_needs_network(e2e)
    assert BUTTON in _fyi(e2e, f"terminal:{cid}")  # offered, the Next: line says what it needs
    assert network_rerun.refusal(e2e.ctx, cid) == "egress_mode"
    assert refusal_text(e2e.ctx, "egress_mode", cid) == (
        "the egress mode is open; rerun with network needs sandbox_open (/egress)"
    )
    with pytest.raises(network_rerun.RerunRefused) as info:
        network_rerun.start_rerun(e2e.ctx, cid, "tg:4242")
    assert info.value.code == "egress_mode" and e2e.state(cid) == "FAILED"


def test_a_rescoped_rerun_loses_its_grant(e2e: E2E, monkeypatch: pytest.MonkeyPatch) -> None:
    """C-D17: the rerun's change hits the ceiling, the owner makes it smaller, the new gate 1's coder is offline."""
    _switches(e2e)
    patch_policy(monkeypatch, e2e.ctx, diff={"max_files": 2})
    e2e.answer("scout", {**PROPOSAL, "estimate": {"files": 2, "code_lines": 10, "test_lines": 10, "class": "s"}},
               {**PROPOSAL, "estimate": {"files": 2, "code_lines": 4, "test_lines": 4, "class": "xs"}})  # fmt: skip
    e2e.answer("coder", NEEDS, NEEDS, CODER)
    cid = _fail_needs_network(e2e)
    e2e.edits = {**EDITS, "src/extra.py": "EXTRA = 1\n"}  # 3 files > the ceiling
    network_rerun.start_rerun(e2e.ctx, cid, "tg:4242")
    assert e2e.run_until(cid, "RESCOPE_REQUIRED") == "RESCOPE_REQUIRED", e2e.why(cid)
    assert _decisions(e2e, cid)[-1] == ("web", "rerun")
    row = db.one(e2e.ctx.conn, "SELECT network_grant, network_grant_version FROM contributions WHERE id = ?", (cid,))
    assert tuple(row) == (0, None)
    ProposalService(e2e.ctx).request_revision(cid, 1, "smaller", "", OWNER)
    e2e.edits = dict(EDITS)
    e2e.start_work(cid)
    assert e2e.run_until(cid, "AWAITING_APPROVAL") == "AWAITING_APPROVAL", e2e.why(cid)
    assert _decisions(e2e, cid)[-1] == ("none", "not_requested") and e2e.exec_networks[-1] == "none"
    approval = e2e.pending_approval(cid)
    action = db.one(e2e.ctx.conn, "SELECT action_id FROM approvals WHERE id = ?", (approval,))[0]
    assert view_for_action(e2e.ctx.conn, str(action), True).network_reasons == ()  # a new round without network
    assert e2e.writes() == []
