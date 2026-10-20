"""``ossctl review-mirror enable|disable|status|purge`` (ADR-0019 C, DESIGN §14.11.14, U9). Operator only.

``enable`` runs the enable-time probes with the gatekeeper's own mirror token (the token never leaves
the gatekeeper) and is human-initiated only; the mirror is active once ``telegram.yaml
review_mirror.enabled: true`` is also loaded through a confirmed ``/reload``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require

router = APIRouter(prefix="/v1")
OP = Role.OPERATOR


def _service(request: Request) -> Any:
    return ctx(request).review_mirror


@router.get("/review-mirror")
async def status(request: Request, caller: Annotated[Caller, Depends(require(OP))]) -> Any:
    svc = _service(request)
    if svc is None:
        return JSONResponse(status_code=503, content={"error": "review_mirror_unavailable"})
    return svc.status()


@router.post("/review-mirror/enable")
async def enable(request: Request, caller: Annotated[Caller, Depends(require(OP))]) -> Any:
    svc = _service(request)
    if svc is None:
        return JSONResponse(status_code=503, content={"error": "review_mirror_unavailable"})
    result = await svc.enable(caller.actor)
    if not result.get("ok"):
        return JSONResponse(status_code=409, content=result)
    ctx(request).operator_fyi("REVIEW MIRROR ENABLE", caller.peer.uid, caller.peer.pid)
    return result


@router.post("/review-mirror/disable")
async def disable(request: Request, caller: Annotated[Caller, Depends(require(OP))]) -> Any:
    svc = _service(request)
    if svc is None:
        return JSONResponse(status_code=503, content={"error": "review_mirror_unavailable"})
    svc.disable(caller.actor, "operator")
    return svc.status()


@router.post("/review-mirror/purge")
async def purge(request: Request, caller: Annotated[Caller, Depends(require(OP))]) -> Any:
    svc = _service(request)
    if svc is None:
        return JSONResponse(status_code=503, content={"error": "review_mirror_unavailable"})
    return await svc.purge(caller.actor)
