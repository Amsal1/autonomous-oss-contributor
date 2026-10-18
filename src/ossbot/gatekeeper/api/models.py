"""Model catalog and global model switch endpoints (DESIGN §16, N6, R4-N3)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from ossbot.common.schemas import CatalogUpsert, ModelSetRequest
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require

router = APIRouter(prefix="/v1")


@router.get("/models/catalog")
async def get_catalog(request: Request, caller: Annotated[Caller, Depends(require())]) -> list[dict[str, Any]]:
    return [{"model_id": e["model_id"], "forced": e["forced"]} for e in ctx(request).models.catalog()]


@router.post("/models/catalog")
async def upsert_catalog(
    request: Request, body: CatalogUpsert, caller: Annotated[Caller, Depends(require(Role.WORKER))]
) -> dict[str, Any]:
    return {"new_ids": ctx(request).models.upsert_catalog(body.models, caller.actor)}


@router.post("/models/refresh", status_code=202)
async def request_refresh(
    request: Request, caller: Annotated[Caller, Depends(require(Role.OPERATOR))]
) -> dict[str, Any]:
    """``ossctl models refresh``: the runner runs the catalog job on its next scheduler tick."""
    return {"requested_at": ctx(request).models.request_refresh(caller.actor)}


@router.post("/models/refresh/claim")
async def claim_refresh(request: Request, caller: Annotated[Caller, Depends(require(Role.WORKER))]) -> dict[str, Any]:
    """Runner: consume a pending refresh request (``requested`` false when there is none)."""
    return {"requested": ctx(request).models.claim_refresh(caller.actor)}


@router.get("/models")
async def get_models(request: Request, caller: Annotated[Caller, Depends(require(Role.OPERATOR))]) -> dict[str, Any]:
    return ctx(request).models.read().model_dump()


@router.put("/models/{role}")
async def set_model(
    request: Request,
    role: str,
    body: ModelSetRequest,
    caller: Annotated[Caller, Depends(require(Role.OPERATOR))],
) -> dict[str, Any]:
    diff = ctx(request).models.set_model(role, body.model, body.effort, body.force, caller.actor)
    return {"diff": diff}
