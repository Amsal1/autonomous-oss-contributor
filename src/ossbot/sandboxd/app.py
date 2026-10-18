"""sandboxd API on ``/run/ossbot/sandboxd.sock`` (root:osssbx 0660; DESIGN §10.2, §10.9).

Callers are identified by ``SO_PEERCRED`` (never a header): only root and ``ossrunner`` may call
any endpoint. sandboxd must not import the gatekeeper, so the small peer-credential protocol
lives here.
"""

from __future__ import annotations

import asyncio
import re
import socket
import struct
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from uvicorn.protocols.http.h11_impl import H11Protocol

from ossbot.common.config import MODEL_ID_RE
from ossbot.sandboxd.jobs import ApiError, JobService, ProbeRunner
from ossbot.sandboxd.probes import PROBES
from ossbot.sandboxd.profiles import CID_RE, RUN_ID_RE, JobSpec

PEER_KEY = "ossbot.peer_uid"


class PeerCredProtocol(H11Protocol):
    def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        sock = transport.get_extra_info("socket")
        uid: int | None = None
        if sock is not None and sock.family == socket.AF_UNIX:
            try:
                raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                uid = struct.unpack("3i", raw)[1]
            except OSError:
                uid = None
        inner = self.app

        async def app(scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
            if scope.get("type") == "http":
                scope = {**scope, PEER_KEY: uid}
            await inner(scope, receive, send)

        self.app = app


class DrainBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    timeout_s: Annotated[int, Field(ge=0, le=3600)]


class ProbeRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    models: dict[str, Annotated[str, Field(pattern=MODEL_ID_RE)]]


def _caller(request: Request) -> int:
    uid = request.scope.get(PEER_KEY)
    if not isinstance(uid, int) or uid not in request.app.state.allowed_uids:
        raise HTTPException(status_code=403, detail="caller not allowed")
    return uid


Caller = Annotated[int, Depends(_caller)]


def create_app(jobs: JobService, probes: ProbeRunner, allowed_uids: frozenset[int]) -> FastAPI:
    app = FastAPI(title="ossbot-sandboxd", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.allowed_uids = allowed_uids

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"error": exc.error_class, "detail": exc.detail}, status_code=exc.status)

    def _check(value: str, pattern: str) -> None:
        if re.fullmatch(pattern, value) is None:
            raise ApiError(400, "bad_id")

    @app.get("/v1/health")
    def health(uid: Caller) -> dict[str, Any]:
        running = jobs.running()
        return {"ok": True, "running": len(running), "running_run_ids": running}

    @app.post("/v1/drain")
    def drain(body: DrainBody, uid: Caller) -> dict[str, Any]:
        """CR-001: blocks up to ``timeout_s`` while running jobs finish, then interrupts the rest.
        Admission is closed by the lifecycle flag (written first by ``ossctl down``)."""
        return jobs.drain(body.timeout_s)

    @app.post("/v1/jobs", status_code=202)
    def submit(spec: JobSpec, uid: Caller) -> dict[str, Any]:
        return jobs.submit(spec)

    @app.get("/v1/jobs/{run_id}")
    def get_job(run_id: str, uid: Caller) -> dict[str, Any]:
        _check(run_id, RUN_ID_RE)
        return jobs.get(run_id)

    @app.get("/v1/jobs/{run_id}/logs")
    def job_logs(run_id: str, uid: Caller, tail: int | None = None) -> PlainTextResponse:
        _check(run_id, RUN_ID_RE)
        if tail is not None and not 1 <= tail <= 100_000:
            raise ApiError(400, "bad_tail")
        return PlainTextResponse(jobs.logs(run_id, tail).decode("utf-8", "replace"))

    @app.delete("/v1/jobs/{run_id}", status_code=202)
    def cancel(run_id: str, uid: Caller) -> dict[str, Any]:
        _check(run_id, RUN_ID_RE)
        jobs.cancel(run_id)
        return {"run_id": run_id, "cancelled": True}

    @app.post("/v1/tasks/{cid}/destroy")
    def destroy(cid: str, uid: Caller) -> dict[str, Any]:
        _check(cid, CID_RE)
        jobs.destroy_task(cid)
        return {"cid": cid, "destroyed": True}

    @app.post("/v1/probes/run")
    def probes_run(body: ProbeRunRequest, uid: Caller) -> dict[str, Any]:
        return {"results": probes.run_all(body.models)}

    @app.get("/v1/probes/status")
    def probes_status(uid: Caller) -> dict[str, Any]:
        key = jobs.probe_key()
        status = jobs.probes.status(key) if key is not None else {}
        return {
            "probe_key": key.sha256 if key is not None else None,
            "trust_mode": key.trust_mode if key is not None else None,
            "probes": {p: status.get(p) for p in PROBES},
            "all_passed": jobs.probes.all_passed(key),
        }

    return app
