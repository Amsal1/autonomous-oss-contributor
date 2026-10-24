"""sandboxd's notices (build-env design §4.4, §4.7, §5.1, ADR-0025): role sandbox takes the worker branch of
``/v1/notify`` (the feed, P1 allowed, the information-sign prefix, no session), with the same 10/h per-caller
limit; a notice is a message, never a go action."""

from __future__ import annotations

import pytest

from ossbot.gatekeeper.context import AppContext
from tests.conftest import UID_SANDBOX, UID_WORKER
from tests.unit.gatekeeper.api_helpers import client

pytestmark = pytest.mark.unit


def _feed(ctx: AppContext) -> list[tuple[str, int]]:
    rows = ctx.conn.execute("SELECT payload_json, priority FROM tg_outbox ORDER BY id").fetchall()
    return [(str(r[0]), int(r[1])) for r in rows]


async def test_sandbox_notice_is_a_p1_feed_message(ctx: AppContext) -> None:
    async with client(ctx, UID_SANDBOX) as c:
        resp = await c.post("/v1/notify", json={"text": "Floor detector down", "priority": "P1"})
    assert resp.status_code == 202 and resp.json() == {"queued": True, "priority": "P1"}
    sent = [(p, prio) for p, prio in _feed(ctx) if "Floor detector down" in p]
    assert len(sent) == 1 and sent[0][1] == 1 and "ℹ️ Floor detector down" in sent[0][0]


async def test_sandbox_notices_are_rate_limited_per_caller(ctx: AppContext) -> None:
    async with client(ctx, UID_SANDBOX) as c:
        for i in range(10):
            assert (await c.post("/v1/notify", json={"text": f"n{i}"})).status_code == 202
        limited = await c.post("/v1/notify", json={"text": "one too many"})
        p0 = await c.post("/v1/notify", json={"text": "x", "priority": "P0"})
    assert (limited.status_code, limited.json()["error"]) == (429, "rate_limited")
    assert p0.status_code in (422, 429)  # never a P0
    async with client(ctx, UID_WORKER) as c:
        assert (await c.post("/v1/notify", json={"text": "worker"})).status_code == 202  # per caller
