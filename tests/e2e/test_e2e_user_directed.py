"""AC 40 end to end: ``ossctl work <url>`` (POST /v1/work) skips ranking, caps, the size preference and
the filters, but still passes both gates; an item above the autonomous size preference still gets
a proposal card (marked 👤) and nothing runs before Start work."""

from __future__ import annotations

from ossbot.gatekeeper import db
from tests.e2e.conftest import E2E, PROPOSAL


def test_user_directed_large_change_gets_a_card(e2e: E2E) -> None:
    created = e2e.ctx.contributions.create_user("https://github.com/langfuse/langfuse/issues/1234",
                                                "fix it properly, with tests", "uid:1000/operator")  # fmt: skip
    cid = str(created["cid"])
    big = {**PROPOSAL, "estimate": {"files": 12, "code_lines": 600, "test_lines": 400, "class": "xl"}}
    e2e.answer("scout", big)
    assert e2e.run_until(cid, "PROPOSED") == "PROPOSED", e2e.why(cid)
    task = e2e.prompts("scout")[0]
    assert "no size preference" in task and "fix it properly, with tests" in task
    row = db.one(e2e.ctx.conn, "SELECT est_class, source FROM contributions WHERE id = ?", (cid,))
    assert row is not None and tuple(row) == ("xl", "user")
    assert e2e.pipeline_log() == ["kiro:vetter", "fetch:scout", "kiro:scout"]
    e2e.answer("scout", PROPOSAL)
    _, approval = e2e.to_preview(cid)
    assert approval and e2e.writes() == []
