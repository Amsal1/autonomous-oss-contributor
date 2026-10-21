"""Hold routes (open-discovery design §13.3): set by sandbox and worker (a stop, I9), listed by worker and
operator, released only by the operator (announced on Telegram); schema errors are 400 ``schema_error``."""

from __future__ import annotations

import json

import pytest

from ossbot.common.schemas import State
from ossbot.gatekeeper.context import AppContext
from tests.conftest import UID_OPERATOR, UID_REQUESTER, UID_SANDBOX, UID_WORKER, insert_contribution
from tests.fakes.holds import hold_body, hold_request
from tests.unit.gatekeeper.api_helpers import client


def _fyis(ctx: AppContext) -> list[str]:
    return [json.loads(r[0])["text"] for r in ctx.conn.execute("SELECT payload_json FROM tg_outbox")]


@pytest.mark.parametrize("uid", [UID_SANDBOX, UID_WORKER])
async def test_sandbox_and_worker_set_a_hold(ctx: AppContext, uid: int) -> None:
    cid = insert_contribution(ctx, State.VALIDATING)
    async with client(ctx, uid) as c:
        first = await c.post(f"/v1/contributions/{cid}/hold", json=hold_body())
        again = await c.post(f"/v1/contributions/{cid}/hold", json=hold_body())
    assert first.status_code == 201 and again.status_code == 200
    assert first.json() == again.json()
    assert first.json()["detail"]["breached"] == ["upload"] and first.json()["released_at"] is None


@pytest.mark.parametrize("uid", [UID_REQUESTER, UID_OPERATOR])
async def test_other_roles_cannot_set_a_hold(ctx: AppContext, uid: int) -> None:
    cid = insert_contribution(ctx, State.VALIDATING)
    async with client(ctx, uid) as c:
        resp = await c.post(f"/v1/contributions/{cid}/hold", json=hold_body())
    assert resp.status_code == 403
    assert not ctx.holds.is_held(cid)


async def test_schema_and_unknown_cid(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.VALIDATING)
    async with client(ctx, UID_SANDBOX) as c:
        bad = await c.post(f"/v1/contributions/{cid}/hold", json=hold_body(top_hosts=["Evil Host"]))
        unknown = await c.post("/v1/contributions/c_0000000000/hold", json=hold_body())
        path = await c.post("/v1/contributions/not-a-cid/hold", json=hold_body())
    assert (bad.status_code, bad.json()["error"]) == (400, "schema_error")
    assert (unknown.status_code, unknown.json()["error"]) == (404, "unknown_cid")
    assert (path.status_code, path.json()["error"]) == (400, "schema_error")


async def test_list_and_release_only_by_the_operator(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.VALIDATING)
    ctx.holds.hold(cid, hold_request(), "uid:2002/sandbox")
    for uid in (UID_SANDBOX, UID_WORKER, UID_REQUESTER):
        async with client(ctx, uid) as c:
            assert (await c.post(f"/v1/contributions/{cid}/hold/release")).status_code == 403
    assert ctx.holds.is_held(cid)
    async with client(ctx, UID_WORKER) as c:
        listed = (await c.get("/v1/holds?open=true")).json()
    assert [h["cid"] for h in listed] == [cid]
    async with client(ctx, UID_OPERATOR) as c:
        released = await c.post(f"/v1/contributions/{cid}/hold/release")
        missing = await c.post(f"/v1/contributions/{cid}/hold/release")
        open_now = (await c.get("/v1/holds")).json()
        every = (await c.get("/v1/holds?open=false")).json()
    assert released.status_code == 200 and released.json()["released_by"].endswith("/operator")
    assert (missing.status_code, missing.json()["error"]) == (404, "no_open_hold")
    assert open_now == [] and [h["cid"] for h in every] == [cid]
    assert not ctx.holds.is_held(cid)
    assert any(f"Local operator action: RESUME HOLD {cid}" in t for t in _fyis(ctx))
