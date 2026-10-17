"""sandboxd API (DESIGN §10.2): peer-uid authorization and the JobSpec endpoints."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from ossbot.sandboxd.app import PEER_KEY, create_app
from ossbot.sandboxd.jobs import ProbeRunner
from tests.unit.sandboxd.conftest import Sbx

RUNNER_UID = 2001


def _client(sbx: Sbx, uid: int | None) -> httpx.AsyncClient:
    app = create_app(sbx.jobs, ProbeRunner(sbx.jobs), frozenset({0, RUNNER_UID}))

    async def with_peer(scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        await app({**scope, PEER_KEY: uid}, receive, send)

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=with_peer), base_url="http://sandboxd")


async def test_unknown_peer_forbidden(sbx: Sbx) -> None:
    for uid in (None, 1001, 1000):
        async with _client(sbx, uid) as client:
            assert (await client.get("/v1/health")).status_code == 403


async def test_runner_can_submit_and_read(sbx: Sbx) -> None:
    body = {
        "run_id": "r1",
        "cid": "c_01ABCDEFGH",
        "profile": "fetch",
        "entrypoint": "fetch",
        "args": {"repo": "owner/repo"},
        "repo_mode": "rw",
        "timeout_s": 60,
    }
    async with _client(sbx, RUNNER_UID) as client:
        resp = await client.post("/v1/jobs", json=body)
        assert resp.status_code == 202, resp.text
        assert (await client.get("/v1/jobs/r1")).json()["state"] == "succeeded"
        assert (await client.get("/v1/jobs/r1/logs", params={"tail": 5})).status_code == 200
        assert (await client.get("/v1/jobs/BAD!")).status_code == 400


async def test_errors_are_structured(sbx: Sbx) -> None:
    body = {
        "run_id": "r1",
        "cid": "c_01ABCDEFGH",
        "profile": "stack",
        "entrypoint": "validate",
        "args": {},
        "repo_mode": "rw",
        "timeout_s": 60,
    }
    async with _client(sbx, 0) as client:
        resp = await client.post("/v1/jobs", json=body)
        assert resp.status_code == 400 and resp.json()["error"] == "bad_spec"
        assert (await client.post("/v1/jobs", json={**body, "evil": 1})).status_code == 422
        assert (await client.get("/v1/jobs/nope")).status_code == 404


async def test_probe_status_reports_tuple(sbx: Sbx) -> None:
    async with _client(sbx, 0) as client:
        doc = (await client.get("/v1/probes/status")).json()
    assert doc["trust_mode"] == "ids" and doc["all_passed"] is False
    assert set(doc["probes"]) == {"research_deny", "symlink", "coder_surface", "settings", "workspace"}


async def test_destroy_requires_terminal(sbx: Sbx) -> None:
    sbx.gatekeeper.states["c_01ABCDEFGH"] = "WATCHING"
    async with _client(sbx, RUNNER_UID) as client:
        resp = await client.post("/v1/tasks/c_01ABCDEFGH/destroy")
    assert resp.status_code == 409 and resp.json()["error"] == "not_terminal"
