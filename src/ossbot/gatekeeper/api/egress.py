"""Egress notes, read-only (build-env design §5.2, ADR-0025): ``GET /v1/egress/notes`` for ``ossctl egress notes``
and the runner. The notes are written by sandboxd through ``POST /v1/contributions/{cid}/egress-notes``
(``api/contributions.py``); nothing here changes state.

``POST /v1/egress/health`` (role sandbox, build-env design §9.2): sandboxd's floor-detector report, kept in memory
for ``/status`` (``egress_status.py``); data only, it gates nothing and is never a go action (I9)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from ossbot.common.schemas import SandboxHealthReport
from ossbot.gatekeeper import egress_status
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require
from ossbot.gatekeeper.egress_notes import LIST_MAX, EgressNotes

router = APIRouter(prefix="/v1")


@router.post("/egress/health")
async def sandbox_health(
    request: Request, body: SandboxHealthReport, caller: Annotated[Caller, Depends(require(Role.SANDBOX))]
) -> dict[str, Any]:
    egress_status.record(ctx(request), body)
    return {"ok": True}


@router.get("/egress/notes")
async def list_notes(
    request: Request,
    caller: Annotated[Caller, Depends(require(Role.WORKER, Role.OPERATOR))],
    cid: Annotated[str | None, Query(pattern=r"^c_[0-9A-Z]{10}$")] = None,
    since: Annotated[str | None, Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=1, le=LIST_MAX)] = 50,
) -> list[dict[str, Any]]:
    c = ctx(request)
    notes = EgressNotes(c.conn, c.clock, c.audit).since(since, cid, limit)
    return [n.model_dump(mode="json") for n in notes]
