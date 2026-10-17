"""Control endpoints: health, state, pause/kill (stop: many roles), resume/dry-run (go: operator)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from ossbot.common.schemas import ControlView, DryRunRequest, ReasonRequest
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require

router = APIRouter(prefix="/v1")
OP, WK, SB, RQ = Role.OPERATOR, Role.WORKER, Role.SANDBOX, Role.REQUESTER


@router.get("/health")
async def health(request: Request, caller: Annotated[Caller, Depends(require())]) -> dict[str, Any]:
    return {"ok": True}


@router.get("/control")
async def get_control(request: Request, caller: Annotated[Caller, Depends(require())]) -> ControlView:
    return ctx(request).control.view()


@router.post("/control/pause")
async def pause(
    request: Request, body: ReasonRequest, caller: Annotated[Caller, Depends(require(OP, WK, SB, RQ))]
) -> ControlView:
    return ctx(request).control.pause(body.reason, caller.actor)


@router.post("/control/kill")
async def kill(
    request: Request, body: ReasonRequest, caller: Annotated[Caller, Depends(require(OP, WK, RQ))]
) -> ControlView:
    return ctx(request).control.kill(body.reason, caller.actor, "soft")


@router.post("/control/resume", status_code=202)
async def resume(request: Request, caller: Annotated[Caller, Depends(require(OP))]) -> dict[str, Any]:
    """ossctl asks for the TTY confirm before calling; the root enforcer removes the flags."""
    c = ctx(request)
    c.control.request_resume(caller.actor, "ossctl")
    c.operator_fyi("RESUME", caller.peer.uid, caller.peer.pid)
    return {"requested": True}


@router.post("/control/dry-run")
async def dry_run(
    request: Request, body: DryRunRequest, caller: Annotated[Caller, Depends(require(OP))]
) -> ControlView:
    c = ctx(request)
    c.control.set_dry_run(body.enabled, caller.actor)
    if not body.enabled:
        c.operator_fyi("DRY-RUN OFF", caller.peer.uid, caller.peer.pid)
    return c.control.view()
