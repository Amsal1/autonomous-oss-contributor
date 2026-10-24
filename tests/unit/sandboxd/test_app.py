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
        "egress": "mode",
    }
    async with _client(sbx, RUNNER_UID) as client:
        resp = await client.post("/v1/jobs", json=body)
        assert resp.status_code == 202, resp.text
        assert (await client.get("/v1/jobs/r1")).json()["state"] == "succeeded"
        assert (await client.get("/v1/jobs/r1/logs", params={"tail": 5})).status_code == 200
        assert (await client.get("/v1/jobs/BAD!")).status_code == 400


async def test_job_document_carries_the_coders_exec_network(sbx: Sbx) -> None:
    """build-env §8.1: GET /v1/jobs/{run_id} returns ``exec_network`` (``web`` for a networked agent-exec, ``none``
    otherwise; null for non-coder jobs), which the runner reads as InvokeResult.exec_network."""
    from tests.unit.sandboxd.test_jobs import _coder, pass_all_probes

    pass_all_probes(sbx)
    sbx.mode[0] = "sandbox_full"
    sbx.gatekeeper.egress_answer = "web"
    sbx.jobs.submit(_coder(sbx, "r3"))
    sbx.gatekeeper.egress_answer = "none"
    sbx.jobs.submit(_coder(sbx, "r4"))
    async with _client(sbx, RUNNER_UID) as client:
        web = (await client.get("/v1/jobs/r3")).json()
        offline = (await client.get("/v1/jobs/r4")).json()
    assert (web["state"], web["exec_network"]) == ("succeeded", "web")
    assert (offline["state"], offline["exec_network"]) == ("succeeded", "none")


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


async def test_pending_holds_listed_and_cleared_by_root_only(sbx: Sbx) -> None:
    """Open-discovery §13.2 (M3): ``ossctl egress holds`` lists them; only root's release clears one."""
    sbx.jobs._pending("c_01ABCDEFGH", "r1", {"run_id": "r1"}, "404:unknown_cid")
    async with _client(sbx, RUNNER_UID) as client:
        listed = (await client.get("/v1/egress/pending")).json()
        assert (await client.delete("/v1/egress/pending/c_01ABCDEFGH")).status_code == 403
    assert [(p["cid"], p["rejected_code"]) for p in listed] == [("c_01ABCDEFGH", "404:unknown_cid")]
    async with _client(sbx, 0) as client:
        assert (await client.delete("/v1/egress/pending/c_01ABCDEFGH")).json() == {"cid": "c_01ABCDEFGH",
                                                                                     "cleared": True}  # fmt: skip
        assert (await client.delete("/v1/egress/pending/c_01ABCDEFGH")).status_code == 404
        assert (await client.delete("/v1/egress/pending/BAD!")).status_code == 400
    assert sbx.jobs.pending_holds() == []


async def test_destroy_requires_terminal(sbx: Sbx) -> None:
    sbx.gatekeeper.states["c_01ABCDEFGH"] = "WATCHING"
    async with _client(sbx, RUNNER_UID) as client:
        resp = await client.post("/v1/tasks/c_01ABCDEFGH/destroy")
    assert resp.status_code == 409 and resp.json()["error"] == "not_terminal"
