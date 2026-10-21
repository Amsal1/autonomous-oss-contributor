"""Routes of design §8.4: roles, answers, refusals as ``{"error": code}`` and 400 ``schema_error``."""

from __future__ import annotations

from typing import Any

from ossbot.common.schemas import ResolvedTargetView
from ossbot.gatekeeper.context import AppContext
from tests.conftest import UID_OPERATOR, UID_REQUESTER, UID_SANDBOX, UID_WORKER
from tests.unit.gatekeeper.api_helpers import client

REPO = "openfaas/faas"  # not in config/targets.yaml


def body(**kw: Any) -> dict[str, Any]:
    return {"repo": REPO, "source": "search", "status": "candidate", "topic": "gitops", "stars": 25000, **kw}


async def test_record_list_and_excluded(ctx: AppContext) -> None:
    async with client(ctx, UID_WORKER) as c:
        resp = await c.post("/v1/discovered-targets", json=body())
        assert resp.status_code == 200 and resp.json()["status"] == "candidate"
        rows = (await c.get("/v1/discovered-targets", params={"status": "candidate", "untried": "true"})).json()
        assert [r["repo"] for r in rows] == [REPO]
        assert (await c.get("/v1/discovered-targets/excluded")).json() == {"repos": [REPO]}
    async with client(ctx, UID_OPERATOR) as c:
        assert len((await c.get("/v1/discovered-targets")).json()) == 1
        assert (await c.get("/v1/discovered-targets/excluded")).status_code == 403
        assert (await c.post("/v1/discovered-targets", json=body())).status_code == 403


async def test_schema_errors_are_400(ctx: AppContext) -> None:
    async with client(ctx, UID_WORKER) as c:
        for bad in (body(status="kept"), body(bogus=1), body(status="skipped"), body(repo="no slug")):
            resp = await c.post("/v1/discovered-targets", json=bad)
            assert resp.status_code == 400 and resp.json()["error"] == "schema_error", bad
        resp = await c.get("/v1/discovered-targets", params={"status": "approved"})
        assert resp.status_code == 400 and resp.json()["error"] == "schema_error"
        resp = await c.get("/v1/discovered-targets", params={"limit": 501})
        assert resp.status_code == 400 and resp.json()["error"] == "schema_error"
        resp = await c.get("/v1/targets/resolve", params={"repo": "bad slug"})
        assert resp.status_code == 400 and resp.json()["error"] == "schema_error"
    async with client(ctx, UID_OPERATOR) as c:
        resp = await c.post("/v1/discovered-targets/decision", json={"repo": REPO, "decision": "approve"})
        assert resp.status_code == 400 and resp.json()["error"] == "schema_error"


async def test_refusals(ctx: AppContext) -> None:
    async with client(ctx, UID_WORKER) as c:
        resp = await c.post("/v1/discovered-targets", json=body(repo="langfuse/langfuse"))
        assert (resp.status_code, resp.json()["error"]) == (409, "is_target")
        resp = await c.post("/v1/discovered-targets", json=body(repo="LineageOS/android"))
        assert (resp.status_code, resp.json()["error"]) == (400, "denied")
        resp = await c.post("/v1/discovered-targets", json=body(kept_check=True))
        assert (resp.status_code, resp.json()["error"]) == (409, "not_kept")
    async with client(ctx, UID_OPERATOR) as c:
        resp = await c.post("/v1/discovered-targets/decision", json={"repo": REPO, "decision": "never"})
        assert resp.status_code == 200 and resp.json()["view"]["status"] == "never"
        resp = await c.post("/v1/discovered-targets/decision", json={"repo": "langfuse/langfuse", "decision": "keep"})
        assert (resp.status_code, resp.json()["error"]) == (409, "already_target")
        resp = await c.post("/v1/discovered-targets/decision",
                            json={"repo": "langfuse/langfuse", "decision": "strict"})  # fmt: skip
        assert (resp.status_code, resp.json()["error"]) == (409, "is_target")
        resp = await c.post("/v1/discovered-targets/decision",
                            json={"repo": "langfuse/langfuse", "decision": "never"})  # fmt: skip
        assert resp.json()["routed"] == "discovery_prefs"
    async with client(ctx, UID_WORKER) as c:
        resp = await c.post("/v1/discovered-targets", json=body())
        assert (resp.status_code, resp.json()["error"]) == (409, "never_repo")
        resp = await c.post("/v1/discovered-targets/decision", json={"repo": REPO, "decision": "keep"})
        assert resp.status_code == 403


async def test_resolve(ctx: AppContext) -> None:
    async with client(ctx, UID_WORKER) as c:
        resp = await c.get("/v1/targets/resolve", params={"repo": REPO})
        assert (resp.status_code, resp.json()) == (404, {"error": "not_a_target"})
        cfg = (await c.get("/v1/targets/resolve", params={"repo": "langfuse/langfuse"})).json()
        assert cfg["source"] == "config" and cfg["target"]["repo"] == "langfuse/langfuse"
        await c.post("/v1/discovered-targets", json=body())
        doc = (await c.get("/v1/targets/resolve", params={"repo": REPO})).json()
        assert doc["source"] == "discovered"
        target = ResolvedTargetView.model_validate(doc["target"]).to_target()
        assert target == ctx.discovered.effective_target(REPO)
    for uid in (UID_OPERATOR, UID_REQUESTER, UID_SANDBOX):
        async with client(ctx, uid) as c:
            assert (await c.get("/v1/targets/resolve", params={"repo": REPO})).status_code == 403
