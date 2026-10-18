"""CR-004 gate 1 end to end (AC 37, DESIGN §9.5a/§9.5b): discover -> vet -> scout -> proposal card;
nothing from the repo runs and no build fetch, plan or implement job exists until Start work is
confirmed; then fetch -> plan -> implement -> validate -> review -> package -> preview card."""

from __future__ import annotations

import json

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.proposals import ProposalService
from tests.e2e.conftest import E2E, EDITS, PROPOSAL
from tests.unit.gatekeeper.test_proposals import OWNER, start
from tests.unit.gatekeeper.test_size_report import patch_policy


def test_proposal_gate_holds_everything_until_start_work(e2e: E2E) -> None:
    created = e2e.ready_scheduler().discover(["langfuse/langfuse"])
    assert len(created) == 1
    cid = created[0]
    assert e2e.run_until(cid, "PROPOSED") == "PROPOSED", e2e.why(cid)
    for _ in range(5):  # waiting at gate 1: nothing else runs for it
        e2e.ready_scheduler().tick()
    assert e2e.state(cid) == "PROPOSED"
    assert e2e.pipeline_log() == ["kiro:vetter", "fetch:scout", "kiro:scout"]
    assert not e2e.has_task_volume(cid)  # gate 1 waits without a task volume (§9.5a rule 4)
    pid = db.one(e2e.ctx.conn, "SELECT id FROM proposal_approvals WHERE cid = ?", (cid,))[0]
    start(e2e.ctx, pid)
    assert e2e.run_until(cid, "AWAITING_APPROVAL") == "AWAITING_APPROVAL", e2e.why(cid)
    assert e2e.pipeline_log()[3:] == ["fetch:build", "kiro:planner", "kiro:coder", "validate", "kiro:reviewer",
                                      "kiro:summarizer", "package"]  # fmt: skip
    assert e2e.writes() == []


def test_back_to_gate_1_from_rescope_required(e2e: E2E, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ceiling pauses the item at /package; 📏 Make it smaller re-scouts from the build tree
    (no second clone into it), the new version waits at gate 1 without a volume, and its Start
    work builds afresh: a new fetch, plan and package, measured on the new change only."""
    patch_policy(monkeypatch, e2e.ctx, diff={"max_files": 2})
    e2e.answer("scout", {**PROPOSAL, "estimate": {"files": 2, "code_lines": 10, "test_lines": 10, "class": "s"}},
               {**PROPOSAL, "estimate": {"files": 2, "code_lines": 4, "test_lines": 4, "class": "xs"}})  # fmt: skip
    e2e.edits = {**EDITS, "src/extra.py": "EXTRA = 1\n"}
    cid = e2e.discover_one()
    e2e.start_work(cid)
    assert e2e.run_until(cid, "RESCOPE_REQUIRED") == "RESCOPE_REQUIRED", e2e.why(cid)  # 3 files > the ceiling
    first = len(e2e.pipeline_log())
    ProposalService(e2e.ctx).request_revision(cid, 1, "smaller", "", OWNER)
    assert e2e.run_until(cid, "PROPOSED") == "PROPOSED", e2e.why(cid)
    assert e2e.pipeline_log()[first:] == ["kiro:scout"]  # the snapshot is the build tree: no clone into it
    assert "earlier attempt" in e2e.prompts("scout")[-1]
    assert not e2e.has_task_volume(cid)  # gate 1 waits without a task volume
    row = db.one(e2e.ctx.conn, "SELECT proposal_version, est_class FROM contributions WHERE id = ?", (cid,))
    assert tuple(row) == (2, "xs")
    e2e.edits = dict(EDITS)  # the smaller change leaves src/extra.py out
    e2e.start_work(cid)
    assert e2e.run_until(cid, "AWAITING_APPROVAL") == "AWAITING_APPROVAL", e2e.why(cid)
    assert e2e.pipeline_log()[first + 1:] == ["fetch:build", "kiro:planner", "kiro:coder", "validate",
                                              "kiro:reviewer", "kiro:summarizer", "package"]  # fmt: skip
    report = json.loads(db.one(e2e.ctx.conn, "SELECT size_report_json FROM contributions WHERE id = ?", (cid,))[0])
    assert report["measured"]["files"] == 2  # a fresh clone: src/extra.py of the first attempt is gone
    assert e2e.writes() == []
