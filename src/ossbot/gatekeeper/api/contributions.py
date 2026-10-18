"""Contribution endpoints (DESIGN §12.2): reads, worker creation/transitions, /package (§12.5), import (§9.13)."""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from ossbot.common.schemas import (
    ContributionCreate,
    ImportRequest,
    PackageRequest,
    PackageTsRequest,
    ProposalSubmit,
    State,
    TransitionRequest,
    TransitionResponse,
)
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require
from ossbot.gatekeeper.imports import Importer, ImportInvalid
from ossbot.gatekeeper.packaging import Packager, PackageRefused, gitleaks_scan
from ossbot.gatekeeper.proposals import ProposalRefused, ProposalService
from ossbot.gatekeeper.state_machine import Actor

router = APIRouter(prefix="/v1")
CID_PATTERN = r"^c_[0-9A-Z]{10}$"


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
    cid = ctx(request).contributions.create(
        body.repo, body.issue_number, body.source, body.score, caller.actor, issue_hash=body.issue_hash
    )
    return {"cid": cid}


@router.post("/contributions/{cid}/proposal", status_code=201)
async def submit_proposal(
    request: Request, cid: str, body: ProposalSubmit, caller: Annotated[Caller, Depends(require(Role.WORKER))]
) -> Any:
    """Scout output (CR-004 §9.5a): stored canonical, versioned, hash-bound; queues the gate-1 card."""
    _check_cid(cid)
    try:
        return ProposalService(ctx(request)).submit(cid, body, caller.actor)
    except ProposalRefused as exc:
        return JSONResponse(status_code=exc.status, content={"error": exc.code, "detail": exc.detail})


@router.get("/contributions/{cid}/proposal")
async def get_proposal(
    request: Request,
    cid: str,
    caller: Annotated[Caller, Depends(require(Role.WORKER, Role.OPERATOR))],
    version: Annotated[int | None, Query(ge=1)] = None,
) -> Any:
    _check_cid(cid)
    try:
        return ProposalService(ctx(request)).get(cid, version)
    except ProposalRefused as exc:
        return JSONResponse(status_code=exc.status, content={"error": exc.code, "detail": exc.detail})


@router.post("/contributions/import", status_code=201)
async def import_contribution(
    request: Request, body: ImportRequest, caller: Annotated[Caller, Depends(require(Role.OPERATOR))]
) -> Any:
    c = ctx(request)
    try:
        cid = Importer(c, c.gitops).import_bundle(body, caller.actor)
    except ImportInvalid as exc:
        return JSONResponse(status_code=422, content={"error": f"import_invalid:{exc.rule}", "detail": str(exc)})
    c.operator_fyi("IMPORT", caller.peer.uid, caller.peer.pid)
    return {"cid": cid}


@router.get("/contributions/{cid}/import-bundle")
async def import_bundle(
    request: Request, cid: str, caller: Annotated[Caller, Depends(require(Role.WORKER))]
) -> Response:
    _check_cid(cid)
    c = ctx(request)
    return Response(content=Importer(c, c.gitops).download(cid), media_type="application/octet-stream")


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
async def package(
    request: Request, cid: str, body: PackageRequest, caller: Annotated[Caller, Depends(require(Role.WORKER))]
) -> JSONResponse:
    _check_cid(cid)
    c = ctx(request)
    packager = Packager(c, c.gitops, scanner=c.gitleaks_scanner or gitleaks_scan)
    try:
        result = packager.package(cid, body, caller.actor)
    except PackageRefused as exc:
        return JSONResponse(status_code=exc.status, content={"error": exc.code, "detail": exc.detail})
    return JSONResponse(status_code=result.status, content=result.body)


@router.post("/contributions/{cid}/package-ts")
async def package_ts(
    request: Request,
    cid: str,
    body: PackageTsRequest,
    caller: Annotated[Caller, Depends(require(Role.WORKER))],
) -> dict[str, Any]:
    _check_cid(cid)
    return {"package_ts": ctx(request).contributions.package_ts(cid, body.rev)}
