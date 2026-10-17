"""Contribution endpoints (DESIGN §12.2). Package/import logic lands in FEAT-002 (501 until then)."""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ossbot.common.schemas import (
    ContributionCreate,
    ImportRequest,
    PackageTsRequest,
    State,
    TransitionRequest,
    TransitionResponse,
)
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require
from ossbot.gatekeeper.state_machine import Actor

router = APIRouter(prefix="/v1")
CID_PATTERN = r"^c_[0-9A-Z]{10}$"


def _not_yet(feature: str) -> JSONResponse:
    return JSONResponse(status_code=501, content={"error": "not_implemented", "detail": f"{feature} lands in FEAT-002"})


def _check_cid(cid: str) -> None:
    if not re.fullmatch(CID_PATTERN, cid):
        raise HTTPException(status_code=404, detail="not found")


@router.get("/contributions")
async def list_contributions(
    request: Request,
    caller: Annotated[Caller, Depends(require())],
    state: Annotated[State | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    return ctx(request).contributions.list(state.value if state else None, limit)


@router.get("/contributions/{cid}")
async def get_contribution(request: Request, cid: str, caller: Annotated[Caller, Depends(require())]) -> dict[str, Any]:
    _check_cid(cid)
    doc = ctx(request).contributions.get(cid, redacted=caller.role is Role.REQUESTER)
    if doc is None:
        raise HTTPException(status_code=404, detail="not found")
    return doc


@router.post("/contributions", status_code=201)
async def create_contribution(
    request: Request, body: ContributionCreate, caller: Annotated[Caller, Depends(require(Role.WORKER))]
) -> dict[str, Any]:
    cid = ctx(request).contributions.create(body.repo, body.issue_number, body.source, body.score, caller.actor)
    return {"cid": cid}


@router.post("/contributions/import")
async def import_contribution(
    request: Request, body: ImportRequest, caller: Annotated[Caller, Depends(require(Role.OPERATOR))]
) -> JSONResponse:
    return _not_yet("operator import")


@router.get("/contributions/{cid}/import-bundle")
async def import_bundle(
    request: Request, cid: str, caller: Annotated[Caller, Depends(require(Role.WORKER))]
) -> JSONResponse:
    return _not_yet("import-bundle")


@router.post("/contributions/{cid}/transition")
async def transition(
    request: Request,
    cid: str,
    body: TransitionRequest,
    caller: Annotated[Caller, Depends(require(Role.WORKER))],
) -> TransitionResponse:
    _check_cid(cid)
    return ctx(request).state_machine.transition(cid, body.from_, body.to, Actor.WORKER, body.evidence, caller.actor)


@router.post("/contributions/{cid}/package")
async def package(request: Request, cid: str, caller: Annotated[Caller, Depends(require(Role.WORKER))]) -> JSONResponse:
    return _not_yet("package verification")


@router.post("/contributions/{cid}/package-ts")
async def package_ts(
    request: Request,
    cid: str,
    body: PackageTsRequest,
    caller: Annotated[Caller, Depends(require(Role.WORKER))],
) -> dict[str, Any]:
    _check_cid(cid)
    return {"package_ts": ctx(request).contributions.package_ts(cid, body.rev)}
