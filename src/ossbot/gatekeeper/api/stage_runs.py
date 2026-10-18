"""Stage-run bookkeeping for the runner (DESIGN §9.3, §12.7): open, heartbeat/close, list."""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ossbot.common.schemas import StageRunOpen, StageRunUpdate
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require

router = APIRouter(prefix="/v1")
RUN_RE = re.compile(r"^r_[0-9A-Z]{10}$")


@router.post("/contributions/{cid}/stage-runs", status_code=201)
async def open_run(
    request: Request, cid: str, body: StageRunOpen, caller: Annotated[Caller, Depends(require(Role.WORKER))]
) -> dict[str, Any]:
    if not re.fullmatch(r"^c_[0-9A-Z]{10}$", cid):
        raise HTTPException(status_code=404, detail="not found")
    return ctx(request).stage_runs.open(cid, body, caller.actor)


@router.post("/stage-runs/{run_id}")
async def update_run(
    request: Request, run_id: str, body: StageRunUpdate, caller: Annotated[Caller, Depends(require(Role.WORKER))]
) -> dict[str, Any]:
    if not RUN_RE.fullmatch(run_id):
        raise HTTPException(status_code=404, detail="not found")
    return ctx(request).stage_runs.update(run_id, body, caller.actor)


@router.get("/stage-runs")
async def list_runs(
    request: Request,
    caller: Annotated[Caller, Depends(require(Role.WORKER, Role.OPERATOR))],
    status: Annotated[str | None, Query(pattern=r"^[a-z]{1,16}$")] = None,
    cid: Annotated[str | None, Query(pattern=r"^c_[0-9A-Z]{10}$")] = None,
) -> list[dict[str, Any]]:
    return ctx(request).stage_runs.list(status, cid)
