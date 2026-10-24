"""Roles from fake SO_PEERCRED uids, the route table (no approve route, I1/I3), operator FYIs (§12.2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute

from ossbot.gatekeeper import db
from ossbot.gatekeeper.app import create_app
from ossbot.gatekeeper.authz import ALL_ROLES, Role, interactive_ancestry
from ossbot.gatekeeper.context import AppContext
from tests.conftest import (
    UID_NOBODY,
    UID_OPERATOR,
    UID_REQUESTER,
    UID_ROOT,
    UID_SANDBOX,
    UID_WORKER,
    insert_contribution,
)
from tests.unit.gatekeeper.api_helpers import client

O, W, S, R = Role.OPERATOR, Role.WORKER, Role.SANDBOX, Role.REQUESTER
ANY = ALL_ROLES
# DESIGN §12.2 endpoint table: (method, path) -> roles.
EXPECTED: dict[tuple[str, str], frozenset[Role]] = {
    ("GET", "/v1/health"): ANY,
    ("GET", "/v1/control"): ANY,
    ("POST", "/v1/control/pause"): frozenset({O, W, S, R}),
    ("POST", "/v1/control/kill"): frozenset({O, W, R}),
    ("POST", "/v1/control/resume"): frozenset({O}),
    ("POST", "/v1/control/dry-run"): frozenset({O}),
    # §11.5 egress mode (Telegram /egress is FEAT-009; the API switch is operator only)
    ("GET", "/v1/control/egress-mode"): ANY,
    ("POST", "/v1/control/egress-mode"): frozenset({O}),
    # §15.5 Hermes capability tier (Telegram /hermes tier is FEAT-009; applying it is FEAT-007)
    ("GET", "/v1/control/hermes-tier"): ANY,
    ("POST", "/v1/control/hermes-tier"): frozenset({O}),
    # ADR-0024 implement network: the setting is a go (operator only); the per-run decision is sandboxd's
    ("GET", "/v1/control/implement-network"): ANY,
    ("POST", "/v1/control/implement-network"): frozenset({O}),
    ("GET", "/v1/contributions/{cid}/implement-egress"): frozenset({W, S}),
    ("POST", "/v1/contributions/{cid}/implement-egress"): frozenset({S}),
    # CR-001 lifecycle (operator only: ossctl up|down|status as root/ossadmin)
    ("GET", "/v1/control/lifecycle"): frozenset({O}),
    ("POST", "/v1/control/drain"): frozenset({O}),
    ("POST", "/v1/control/down"): frozenset({O}),
    ("POST", "/v1/control/up"): frozenset({O}),
    # PLAN item 42: workers down / up through the root helper (Telegram /down, /up are FEAT-009)
    ("POST", "/v1/control/lifecycle-request"): frozenset({O}),
    # PLAN item 43: ossctl backup records each archive (metadata only)
    ("POST", "/v1/audit/backup-created"): frozenset({O}),
    ("GET", "/v1/config/snapshot"): ANY,
    ("POST", "/v1/config/reload"): frozenset({O}),
    ("GET", "/v1/contributions"): ANY,
    ("GET", "/v1/contributions/{cid}"): ANY,
    ("POST", "/v1/contributions"): frozenset({W}),
    ("POST", "/v1/contributions/import"): frozenset({O}),
    ("GET", "/v1/contributions/{cid}/import-bundle"): frozenset({W}),
    # ADR-0006 addendum: commit signing status (never the key) and the signed head of a rev (fix rounds)
    ("GET", "/v1/signing"): frozenset({W, O}),
    ("GET", "/v1/contributions/{cid}/head-bundle"): frozenset({W}),
    ("POST", "/v1/contributions/{cid}/transition"): frozenset({W}),
    ("POST", "/v1/contributions/{cid}/package"): frozenset({W}),
    ("POST", "/v1/contributions/{cid}/package-ts"): frozenset({W}),
    ("POST", "/v1/actions"): frozenset({W}),
    ("POST", "/v1/sandbox/probe-result"): frozenset({S}),
    ("GET", "/v1/models/catalog"): ANY,
    ("POST", "/v1/models/catalog"): frozenset({W}),
    ("POST", "/v1/discovery-requests"): frozenset({R, O}),
    ("POST", "/v1/notify"): frozenset({W, R, S}),  # S: sandboxd's floor notices (ADR-0025), the worker branch
    ("POST", "/v1/features/{feature}/cards"): frozenset({R}),  # FEAT-010 item 49: Hermes feature cards (U12)
    ("POST", "/v1/reminders"): frozenset({R}),  # FEAT-010 item 50: ossbot_reminder_create (U12)
    ("GET", "/v1/review-mirror"): frozenset({O}),  # FEAT-010 item 51: ossctl review-mirror (ADR-0019 C)
    ("POST", "/v1/review-mirror/enable"): frozenset({O}),
    ("POST", "/v1/review-mirror/disable"): frozenset({O}),
    ("POST", "/v1/review-mirror/purge"): frozenset({O}),
    ("POST", "/v1/retention/run"): frozenset({O}),  # DESIGN §18.10: ossctl gc (gatekeeper-owned data)
    ("GET", "/v1/models"): frozenset({O}),
    ("PUT", "/v1/models/{role}"): frozenset({O}),
    ("POST", "/v1/models/refresh"): frozenset({O}),
    ("POST", "/v1/models/refresh/claim"): frozenset({W}),
    ("POST", "/v1/contributions/{cid}/proposal"): frozenset({W}),
    ("GET", "/v1/contributions/{cid}/proposal"): frozenset({W, O}),
    ("POST", "/v1/work"): frozenset({O}),
    ("POST", "/v1/discover"): frozenset({O}),
    ("POST", "/v1/discover/claim"): frozenset({W}),
    ("GET", "/v1/discovery-prefs"): frozenset({O, W}),
    ("PUT", "/v1/discovery-prefs"): frozenset({O}),
    ("POST", "/v1/discovery-prefs/undo"): frozenset({O}),
    ("POST", "/v1/contributions/{cid}/stage-runs"): frozenset({W}),
    ("POST", "/v1/stage-runs/{run_id}"): frozenset({W}),
    ("GET", "/v1/stage-runs"): frozenset({W, O, R}),  # R: Hermes ossbot_logs (§15.1)
    ("POST", "/v1/chat/selftest"): frozenset({O}),  # §15.6, PLAN item 37
    ("POST", "/v1/chat/jobs/claim"): frozenset({W}),  # the runner's extract_text jobs for PDF turns
    ("POST", "/v1/chat/jobs/{job_id}/result"): frozenset({W}),
    ("POST", "/v1/chat/sandbox-jobs"): frozenset({R}),  # Hermes code jobs (F007-R1-2 owner decision)
    ("GET", "/v1/chat/sandbox-jobs/{job_id}"): frozenset({R}),
    ("GET", "/v1/audit/verify"): frozenset({O}),
    # post-v1 open discovery (design §8.4)
    ("GET", "/v1/discovered-targets"): frozenset({W, O}),
    ("GET", "/v1/discovered-targets/excluded"): frozenset({W}),
    ("POST", "/v1/discovered-targets"): frozenset({W}),
    ("POST", "/v1/discovered-targets/decision"): frozenset({O}),
    ("POST", "/v1/discovered-targets/untry"): frozenset({O}),  # discovery pacing (2026-10-22): a go
    ("POST", "/v1/discovery-cycles"): frozenset({W}),  # discovery fix 5: cycle reports, record + FYI only
    ("GET", "/v1/targets/resolve"): frozenset({W}),
    # open-discovery design §13.3: a hold is a stop (sandbox, worker); its release is a go (operator)
    ("POST", "/v1/contributions/{cid}/hold"): frozenset({S, W}),
    ("GET", "/v1/holds"): frozenset({W, O}),
    ("POST", "/v1/contributions/{cid}/hold/release"): frozenset({O}),
    # build-env design §5.2 (ADR-0025): sandboxd records egress notes (data, no transition); read-only list
    ("POST", "/v1/contributions/{cid}/egress-notes"): frozenset({S}),
    ("GET", "/v1/egress/notes"): frozenset({W, O}),
    # build-env design §9.2: sandboxd's floor-detector report for /status (data only, gates nothing)
    ("POST", "/v1/egress/health"): frozenset({S}),
}
UIDS = {O: UID_OPERATOR, W: UID_WORKER, S: UID_SANDBOX, R: UID_REQUESTER}


def api_routes(routes: list[Any]) -> list[APIRoute]:
    """Flatten routes; FastAPI 0.143 keeps included routers as lazy wrappers (``original_router``)."""
    out: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            out.append(route)
        elif hasattr(route, "original_router"):
            out.extend(api_routes(route.original_router.routes))
    return out


def route_table(ctx: AppContext) -> dict[tuple[str, str], frozenset[Role]]:
    table: dict[tuple[str, str], frozenset[Role]] = {}
    routes = api_routes(create_app(ctx, housekeeping=False).routes)
    assert routes, "no API routes found"
    for route in routes:
        roles = [d.call.allowed_roles for d in route.dependant.dependencies if hasattr(d.call, "allowed_roles")]
        assert len(roles) == 1, f"{route.path} has no role dependency"
        for method in route.methods:
            table[(method, route.path)] = roles[0]
    return table


def test_route_table_matches_design(ctx: AppContext) -> None:
    assert route_table(ctx) == EXPECTED


def test_only_control_writes_implement_network() -> None:
    """ADR-0024 / I9: the one SQL writer of ``control_state.implement_network`` is ``gatekeeper/control.py``."""
    import ast

    src = Path(__file__).resolve().parents[3] / "src" / "ossbot"
    writers = []
    for path in src.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                sql = " ".join(node.value.split()).upper()
                if "UPDATE CONTROL_STATE" in sql and "IMPLEMENT_NETWORK" in sql:
                    writers.append(path.relative_to(src).as_posix())
    assert writers == ["gatekeeper/control.py"]


def test_there_is_no_approve_route(ctx: AppContext) -> None:
    """I1/I3: approvals come only from Telegram callbacks inside the gatekeeper."""
    for _, path in route_table(ctx):
        assert "approve" not in path and "confirm" not in path and "approval" not in path


@pytest.mark.parametrize("uid", [UID_NOBODY, 4321, None])
async def test_unknown_or_missing_peer_is_403(ctx: AppContext, uid: int | None) -> None:
    async with client(ctx, uid) as c:
        resp = await c.get("/v1/health")
    assert resp.status_code == 403


async def test_root_is_operator(ctx: AppContext) -> None:
    async with client(ctx, UID_ROOT) as c:
        assert (await c.get("/v1/audit/verify")).status_code == 200


@pytest.mark.parametrize("role", [W, S, R])
async def test_non_operator_resume_is_403_and_audited(ctx: AppContext, role: Role) -> None:
    ctx.control.pause("x", "test")
    async with client(ctx, UIDS[role]) as c:
        resp = await c.post("/v1/control/resume")
    assert resp.status_code == 403
    assert not ctx.paths.resume_request.exists()
    row = ctx.conn.execute("SELECT actor FROM audit_events WHERE event = 'forbidden_call'").fetchone()
    assert row is not None and row["actor"] == f"uid:{UIDS[role]}"


def outbox_texts(ctx: AppContext) -> list[str]:
    return [json.loads(r["payload_json"])["text"] for r in ctx.conn.execute("SELECT payload_json FROM tg_outbox")]


async def test_operator_go_actions_emit_fyi(ctx: AppContext) -> None:
    async with client(ctx, UID_OPERATOR) as c:
        assert (await c.post("/v1/control/pause", json={"reason": "maintenance"})).status_code == 200
        assert (await c.post("/v1/control/resume")).status_code == 202
        assert (await c.post("/v1/control/dry-run", json={"enabled": False})).status_code == 200
        policy = ctx.paths.config_dir / "policy.yaml"
        policy.write_text(policy.read_text().replace("comments_per_24h: 10", "comments_per_24h: 8"))
        preview = (await c.post("/v1/config/reload", json={})).json()
        assert preview["applied"] is False and "comments_per_24h" in preview["diff"]
        applied = await c.post("/v1/config/reload", json={"confirm_hash": preview["hash"]})
        assert applied.status_code == 200 and applied.json()["applied"] is True
    texts = outbox_texts(ctx)
    for action in ("RESUME", "DRY-RUN OFF", "CONFIG RELOAD"):
        assert any(f"Local operator action: {action} by uid {UID_OPERATOR}" in t for t in texts), action


async def test_dry_run_on_is_immediate_without_fyi(ctx: AppContext) -> None:
    ctx.control.set_dry_run(False, "test")
    async with client(ctx, UID_OPERATOR) as c:
        assert (await c.post("/v1/control/dry-run", json={"enabled": True})).json()["dry_run"] is True
    assert not any("DRY-RUN" in t for t in outbox_texts(ctx))


async def test_worker_and_requester_can_stop(ctx: AppContext) -> None:
    async with client(ctx, UID_REQUESTER) as c:
        assert (await c.post("/v1/control/pause", json={"reason": "hermes"})).json()["state"] == "paused"
        assert (await c.post("/v1/control/kill", json={"reason": "hermes"})).json()["state"] == "killed"
    async with client(ctx, UID_SANDBOX) as c:
        assert (await c.post("/v1/control/kill", json={"reason": "x"})).status_code == 403


async def test_requester_gets_redacted_view(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, plan_json='{"secret": "plan"}', pr_body="body")
    async with client(ctx, UID_REQUESTER) as c:
        doc = (await c.get(f"/v1/contributions/{cid}")).json()
    assert "plan_json" not in doc and "pr_body" not in doc and doc["id"] == cid
    async with client(ctx, UID_OPERATOR) as c:
        assert "plan_json" in (await c.get(f"/v1/contributions/{cid}")).json()


async def test_transition_via_api_worker_only(ctx: AppContext) -> None:
    cid = insert_contribution(ctx)
    body = {"from": "DISCOVERED", "to": "VETTING", "evidence": {}}
    async with client(ctx, UID_OPERATOR) as c:
        assert (await c.post(f"/v1/contributions/{cid}/transition", json=body)).status_code == 403
    async with client(ctx, UID_WORKER) as c:
        resp = await c.post(f"/v1/contributions/{cid}/transition", json=body)
        assert resp.status_code == 200 and resp.json()["state"] == "VETTING"
        gate = await c.post(f"/v1/contributions/{cid}/transition", json={"from": "VETTING", "to": "AWAITING_APPROVAL"})
        assert gate.status_code == 409
        extra = await c.post(f"/v1/contributions/{cid}/transition", json={**body, "approve": True})
        assert extra.status_code == 400  # extra="forbid"


async def test_notify_rate_limited(ctx: AppContext) -> None:
    assert ctx.chat is not None
    ctx.chat.service.turns.start("tg-4242-1", 1)  # Hermes notices need an active turn (U12)
    headers = {"X-Hermes-Session-Key": "tg-4242-1"}
    async with client(ctx, UID_REQUESTER) as c:
        for _ in range(10):
            assert (await c.post("/v1/notify", json={"text": "hi"}, headers=headers)).status_code == 202
        assert (await c.post("/v1/notify", json={"text": "hi"}, headers=headers)).status_code == 429
    async with client(ctx, UID_WORKER) as c:
        assert (await c.post("/v1/notify", json={"text": "hi"})).status_code == 202  # per caller


async def test_package_and_import_routes_are_implemented(ctx: AppContext) -> None:
    """FEAT-002 replaced the 501 stubs: schema errors and state conflicts, never 501."""
    cid = insert_contribution(ctx, source="operator", issue_number=None)
    async with client(ctx, UID_WORKER) as c:
        assert (await c.post(f"/v1/contributions/{cid}/package")).status_code == 400  # no body
        assert (await c.get(f"/v1/contributions/{cid}/import-bundle")).status_code == 409  # not IMPORTED


async def test_actions_create_pending_approval(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, issue_number=77)
    async with client(ctx, UID_WORKER) as c:
        resp = await c.post("/v1/actions", json={"cid": cid, "kind": "issue_claim", "target_number": 77,
                                                 "body": "I'd like to work on this.\r\n"})  # fmt: skip
        assert resp.status_code == 201, resp.text
        bad = await c.post("/v1/actions", json={"cid": cid, "kind": "push_update", "target_number": 77, "body": "x"})
        assert bad.status_code == 400
        wrong = await c.post("/v1/actions", json={"cid": cid, "kind": "comment", "target_number": 5, "body": "x"})
        assert wrong.status_code == 400
        leak = await c.post("/v1/actions", json={"cid": cid, "kind": "comment", "target_number": 77,
                                                 "body": "ping test-work-login"})  # fmt: skip
        assert leak.status_code == 422 and "confidential" in leak.json()["violations"]
    data = resp.json()
    row = db.one(ctx.conn, "SELECT payload_json FROM outbound_actions WHERE id = ?", (data["action_id"],))
    assert row is not None
    body = json.loads(row[0])["body"]
    assert "\r" not in body and body.endswith(f"<!-- ossbot:action={data['action_id']} -->")
    approval = db.one(ctx.conn, "SELECT status, tg_chat_id FROM approvals WHERE id = ?", (data["approval_id"],))
    assert approval is not None and approval["status"] == "pending" and approval["tg_chat_id"] == 4242


async def test_actions_refused_while_killed(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, issue_number=78)
    ctx.control.kill("x", "test")
    async with client(ctx, UID_WORKER) as c:
        resp = await c.post("/v1/actions", json={"cid": cid, "kind": "comment", "target_number": 78, "body": "x"})
    assert resp.status_code == 423


async def test_discovery_request(ctx: AppContext) -> None:
    async with client(ctx, UID_REQUESTER) as c:
        ok = await c.post("/v1/discovery-requests", json={"url": "https://github.com/langfuse/langfuse/issues/9"})
        assert ok.status_code == 201
        pr = await c.post("/v1/discovery-requests", json={"url": "https://github.com/langfuse/langfuse/pull/9"})
        assert pr.status_code == 400 and pr.json()["error"] == "schema_error"
        # post-v1 open discovery (design §11): a non-target repo becomes a suggestion the runner checks
        other = await c.post("/v1/discovery-requests", json={"url": "https://github.com/nobody/x/issues/1"})
        assert other.status_code == 202 and other.json() == {"queued": True, "repo": "nobody/x", "status": "suggested"}


async def test_schema_errors_are_400(ctx: AppContext) -> None:
    async with client(ctx, UID_OPERATOR) as c:
        resp = await c.post("/v1/control/pause", json={"reason": ""})
    assert resp.status_code == 400 and resp.json()["error"] == "schema_error"


def test_interactive_ancestry(tmp_path: Path) -> None:
    def proc(pid: int, ppid: int, tty: int) -> None:
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "stat").write_text(f"{pid} (some (odd) name) S {ppid} 1 1 {tty} 0 0\n")

    proc(300, 200, 0)
    proc(200, 100, 34816)
    proc(100, 1, 0)
    proc(400, 1, 0)
    assert interactive_ancestry(300, proc=str(tmp_path)) is True
    assert interactive_ancestry(400, proc=str(tmp_path)) is False
    assert interactive_ancestry(999, proc=str(tmp_path)) is False
