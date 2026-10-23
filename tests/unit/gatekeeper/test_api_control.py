"""``/v1/control/implement-network`` (ADR-0024, R2.2): GET for any role, POST for the operator only."""

from __future__ import annotations

import json

from ossbot.gatekeeper.context import AppContext
from tests.conftest import UID_OPERATOR, UID_REQUESTER, UID_SANDBOX, UID_WORKER
from tests.unit.gatekeeper.api_helpers import client

PATH = "/v1/control/implement-network"


def _audits(ctx: AppContext) -> list[dict[str, object]]:
    rows = ctx.conn.execute("SELECT data_json FROM audit_events WHERE event = 'implement_network_changed'")
    return [json.loads(r["data_json"]) for r in rows]


async def test_get_any_role_post_operator_only(ctx: AppContext) -> None:
    # worker (runner), sandbox (sandboxd), requester (Hermes): never a writer (I9)
    for uid in (UID_WORKER, UID_SANDBOX, UID_REQUESTER):
        async with client(ctx, uid) as c:
            assert (await c.post(PATH, json={"value": "auto"})).status_code == 403
            got = await c.get(PATH)
            assert got.status_code == 200
            assert got.json() == {"value": "off", "active": False, "egress_mode": "open", "previous": None}
    assert ctx.control.implement_network() == "off" and _audits(ctx) == []
    async with client(ctx, UID_OPERATOR) as c:
        resp = await c.post(PATH, json={"value": "on_request"})
    assert resp.status_code == 200
    assert resp.json() == {"value": "on_request", "active": False, "egress_mode": "open", "previous": "off"}
    assert _audits(ctx) == [{"from": "off", "to": "on_request", "egress_mode": "open"}]


async def test_post_rejects_bad_bodies(ctx: AppContext) -> None:
    async with client(ctx, UID_OPERATOR) as c:
        for body in ({"value": "on"}, {"value": "auto", "extra": 1}, {}):
            assert (await c.post(PATH, json=body)).status_code == 400, body
    assert ctx.control.implement_network() == "off"


async def test_active_only_in_sandbox_open(ctx: AppContext) -> None:
    default = ctx.config.egress.mode_default
    ctx.control.set_egress_mode("sandbox_open", "tg:4242", default, egress=ctx.config.egress)
    ctx.control.set_implement_network("auto", "tg:4242", default)
    async with client(ctx, UID_WORKER) as c:
        view = (await c.get(PATH)).json()
    assert view == {"value": "auto", "active": True, "egress_mode": "sandbox_open", "previous": None}
