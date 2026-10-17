"""Live claim at package time (DESIGN §9.2 N5): with dry-run off a dry_run_done claim does not count."""

from __future__ import annotations

import json

from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from tests.conftest import insert_action, insert_contribution
from tests.githarness import GitWorld, package_request, stage_artifacts
from tests.unit.gatekeeper.test_package_verify import post

CLAIM_VETTING = json.dumps({"ai_policy": "allowed", "requires_claim_comment": True})


def _setup(gctx: AppContext, world: GitWorld, *, with_claim: bool = True) -> tuple[str, dict[str, object]]:
    cid = insert_contribution(gctx, State.PACKAGING, issue_number=12, vetting_json=CLAIM_VETTING)
    if with_claim:
        insert_action(gctx, cid, "issue_claim",
                      {"target_number": 12, "body": "I'd like to work on this.\n\n<!-- ossbot:action=x_0000000000 -->"},
                      status="dry_run_done")  # fmt: skip
    out = world.package(world.base, {"src/app.py": "print('claim')\n"})
    sums = stage_artifacts(gctx.paths, cid, 1, out.out)
    return cid, package_request(gctx.paths, cid, 1, sums, out.head, world.base)


def _actions(gctx: AppContext, cid: str, kind: str) -> list[dict[str, object]]:
    rows = gctx.conn.execute(
        "SELECT * FROM outbound_actions WHERE cid = ? AND kind = ? ORDER BY created_at", (cid, kind)
    ).fetchall()
    return [dict(r) for r in rows]


async def test_dry_run_claim_counts_while_dry_run(gctx: AppContext, world: GitWorld) -> None:
    cid, body = _setup(gctx, world)
    resp = await post(gctx, cid, body)
    assert resp.status_code == 200
    assert resp.json()["kind"] == "open_pr"


async def test_live_claim_required_then_open_pr(gctx: AppContext, world: GitWorld) -> None:
    cid, body = _setup(gctx, world)
    gctx.control.set_dry_run(False, "uid:1000/operator")
    resp = await post(gctx, cid, body)
    assert resp.status_code == 202, resp.text
    assert resp.json()["state_reason"] == "awaiting_live_claim"
    claims = _actions(gctx, cid, "issue_claim")
    fresh = claims[-1]
    assert fresh["status"] == "pending_approval"
    payload = json.loads(str(fresh["payload_json"]))
    assert payload["body"].endswith(f"<!-- ossbot:action={fresh['id']} -->")
    assert payload["body"].count("ossbot:action=") == 1
    assert _actions(gctx, cid, "open_pr") == []
    row = db.one(gctx.conn, "SELECT state, state_reason FROM contributions WHERE id = ?", (cid,))
    assert (row["state"], row["state_reason"]) == ("PACKAGING", "awaiting_live_claim")
    approvals = gctx.conn.execute("SELECT COUNT(*) FROM approvals WHERE action_id = ?", (fresh["id"],)).fetchone()[0]
    assert approvals == 1

    again = await post(gctx, cid, body)  # same rev, idempotent: no second claim
    assert again.status_code == 202
    assert again.json()["action_id"] == fresh["id"]
    assert len(_actions(gctx, cid, "issue_claim")) == len(claims)

    with db.transaction(gctx.conn):
        gctx.conn.execute("UPDATE outbound_actions SET status = 'done' WHERE id = ?", (fresh["id"],))
    done = await post(gctx, cid, body)
    assert done.status_code == 200, done.text
    assert done.json()["kind"] == "open_pr"
    assert len(_actions(gctx, cid, "open_pr")) == 1


async def test_live_claim_without_any_claim_body(gctx: AppContext, world: GitWorld) -> None:
    cid, body = _setup(gctx, world, with_claim=False)
    gctx.control.set_dry_run(False, "uid:1000/operator")
    resp = await post(gctx, cid, body)
    assert resp.status_code == 409
    assert resp.json()["error"] == "claim_body_missing"
