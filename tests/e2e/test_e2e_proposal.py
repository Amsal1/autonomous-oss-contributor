"""CR-004 gate 1 end to end (AC 37, DESIGN §9.5a/§9.5b): discover -> vet -> scout -> proposal card;
nothing from the repo runs and no build fetch, plan or implement job exists until Start work is
confirmed; then fetch -> plan -> implement -> validate -> review -> package -> preview card."""

from __future__ import annotations

from ossbot.gatekeeper import db
from tests.e2e.conftest import E2E
from tests.unit.gatekeeper.test_proposals import start


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
