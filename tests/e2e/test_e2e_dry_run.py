"""AC 4 and AC 30 end to end: dry-run ON -> preview card labelled DRY RUN -> approve + confirm ->
DRY_RUN_DONE with zero writes on the fake GitHub and the "would" payload in the audit log; after
/dryrun off, discovery creates a new contribution for the same issue."""

from __future__ import annotations

import json

from ossbot.gatekeeper import db
from tests.e2e.conftest import E2E


def test_dry_run_full_pipeline_makes_zero_writes(e2e: E2E) -> None:
    cid, approval = e2e.to_preview()
    card = db.one(e2e.ctx.conn, "SELECT dry_run_shown FROM approvals WHERE id = ?", (approval,))
    assert card is not None and card[0] == 1
    assert e2e.approve(approval) == "dry_run_done"
    assert any("DRY RUN" in r[0] for r in e2e.ctx.conn.execute("SELECT payload_json FROM tg_outbox"))
    assert e2e.state(cid) == "DRY_RUN_DONE"
    assert e2e.writes() == [] and e2e.pusher.pushes == []
    would = db.one(e2e.ctx.conn, "SELECT data_json FROM audit_events WHERE event = 'dry_run_would'")
    assert would is not None and "pull_request" in json.loads(would[0])["would"]
    # AC 30: after dry-run off the same issue is discoverable again
    assert e2e.ready_scheduler().discover(["langfuse/langfuse"]) == []  # still a dry-run rehearsal
    e2e.ctx.control.set_dry_run(False, "uid:1000/operator")
    e2e.ready_scheduler().refused.clear()  # refusals are cached for one discovery cycle
    again = e2e.ready_scheduler().discover(["langfuse/langfuse"])
    assert len(again) == 1 and again[0] != cid
