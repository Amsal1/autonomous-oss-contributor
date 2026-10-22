"""``POST /v1/discovery-cycles`` (discovery fix 5): worker only, ``DiscoveryCycleReport`` (extra=forbid,
bounded), one ``discovery_cycle`` audit row per report, the Discovery FYI only for a finished cycle, and no
state change (no transition, no approval, no control flag)."""

from __future__ import annotations

import json
from typing import Any

from ossbot.common.clock import to_iso
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.api import discovery_cycles
from ossbot.gatekeeper.context import AppContext
from tests.conftest import UID_OPERATOR, UID_REQUESTER, UID_SANDBOX, UID_WORKER, insert_contribution
from tests.unit.gatekeeper.api_helpers import client


def report(ctx: AppContext, **kw: Any) -> dict[str, Any]:
    now = to_iso(ctx.clock.now())
    return {"outcome": "finished", "scope": "full", "cut_reason": None, "started_at": now, "ended_at": now,
            "candidates": 4, "open_candidates": 1, "created_total": 0, "drops": {"stale_issue": 2}, **kw}  # fmt: skip


def audit_rows(ctx: AppContext) -> list[dict[str, Any]]:
    rows = ctx.conn.execute("SELECT data_json FROM audit_events WHERE event = 'discovery_cycle' ORDER BY seq")
    return [json.loads(r[0]) for r in rows]


def fyis(ctx: AppContext) -> list[Any]:
    return list(ctx.conn.execute("SELECT * FROM tg_outbox WHERE dedupe_key LIKE 'discovery_cycle:%'"))


async def test_worker_records_and_a_finished_cycle_gets_one_fyi(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.DISCOVERED, upstream="langfuse/langfuse", issue_number=9)
    before = db.one(ctx.conn, "SELECT state, updated_at FROM contributions WHERE id = ?", (cid,))
    control = ctx.control.view()
    async with client(ctx, UID_WORKER) as c:
        resp = await c.post("/v1/discovery-cycles", json=report(ctx, created=[cid], created_total=1))
        assert (resp.status_code, resp.json()) == (200, {"recorded": True})
        resp = await c.post("/v1/discovery-cycles", json=report(ctx, outcome="cut", cut_reason="search_limit"))
        assert resp.status_code == 200
    assert [(r["outcome"], r["cut_reason"]) for r in audit_rows(ctx)] == [("finished", None), ("cut", "search_limit")]
    (fyi,) = fyis(ctx)  # none for the cut
    assert (fyi["priority"], fyi["thread_key"]) == (2, "feed")
    assert f"<code>{cid}</code>" in fyi["payload_json"]
    after = db.one(ctx.conn, "SELECT state, updated_at FROM contributions WHERE id = ?", (cid,))
    view = ctx.control.view()
    assert tuple(after) == tuple(before)  # nothing changed state
    assert (view.state, view.dry_run, view.kill_level, view.lifecycle) == (
        control.state,
        control.dry_run,
        control.kill_level,
        control.lifecycle,
    )


async def test_only_the_worker_may_report(ctx: AppContext) -> None:
    for uid in (UID_OPERATOR, UID_REQUESTER, UID_SANDBOX):
        async with client(ctx, uid) as c:
            assert (await c.post("/v1/discovery-cycles", json=report(ctx))).status_code == 403, uid
    assert audit_rows(ctx) == []


async def test_schema_errors_are_400_without_an_echo(ctx: AppContext) -> None:
    async with client(ctx, UID_WORKER) as c:
        for bad in (report(ctx, bogus=1), report(ctx, outcome="cut"), report(ctx, cut_reason="stopped"),
                    report(ctx, candidates=100_001), report(ctx, drops={"<b>x</b>": 1}),
                    report(ctx, created=["ghp_notacid"])):  # fmt: skip
            resp = await c.post("/v1/discovery-cycles", json=bad)
            assert resp.status_code == 400 and resp.json()["error"] == "schema_error", bad
            assert "ghp_notacid" not in json.dumps(resp.json())  # loc and msg only, never the value
    assert audit_rows(ctx) == []


async def test_reports_are_rate_limited_per_hour(ctx: AppContext, monkeypatch: Any) -> None:
    monkeypatch.setattr(discovery_cycles, "REPORTS_PER_HOUR", 2)
    async with client(ctx, UID_WORKER) as c:
        codes = [(await c.post("/v1/discovery-cycles", json=report(ctx))).status_code for _ in range(3)]
    assert codes == [200, 200, 429] and len(audit_rows(ctx)) == 2
