"""``POST /v1/sandbox/probe-result`` (owner decision B, FEAT-006 item 32): an inconclusive probe (the
model refused three times) sends one P1 alert and kills nothing; pass and fail send no such alert."""

from __future__ import annotations

import json

import pytest

from ossbot.gatekeeper.context import AppContext
from tests.conftest import UID_SANDBOX
from tests.unit.gatekeeper.api_helpers import client

BODY = {"probe_key": "a" * 64, "probe": "research_deny", "passed": False, "evidence_sha256": "b" * 64}


def texts(ctx: AppContext) -> list[str]:
    return [json.loads(r[0]).get("text", "") for r in ctx.conn.execute("SELECT payload_json FROM tg_outbox")]


@pytest.mark.parametrize(("verdict", "alerts"), [("inconclusive", 1), ("fail", 0), ("pass", 0), (None, 0)])
async def test_inconclusive_probe_alerts_p1_without_a_kill(ctx: AppContext, verdict: str | None, alerts: int) -> None:
    async with client(ctx, UID_SANDBOX) as c:
        resp = await c.post("/v1/sandbox/probe-result", json={**BODY, "verdict": verdict, "attempts": 3})
    assert resp.status_code == 204
    p1 = [t for t in texts(ctx) if "P1 safety probe research_deny inconclusive" in t]
    assert len(p1) == alerts
    assert ctx.control.view().state == "running"  # never a kill or a pause
