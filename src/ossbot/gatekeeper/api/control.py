"""Control endpoints: health, state, pause/kill (stop: many roles), resume/dry-run (go: operator)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from ossbot.common.schemas import (
    ControlView,
    DownRequest,
    DrainRequest,
    DryRunRequest,
    EgressModeRequest,
    EgressModeView,
    HermesTierRequest,
    HermesTierView,
    ReasonRequest,
    UpRequest,
)
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require
from ossbot.gatekeeper.chat.hermes_surface import allowed_toolsets

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


@router.get("/control/lifecycle")
async def get_lifecycle(request: Request, caller: Annotated[Caller, Depends(require(OP))]) -> dict[str, Any]:
    """CR-001: lifecycle, running work, pending approvals, Telegram poll health, egress mode and
    Hermes tier (status.ps1, PLAN item 39)."""
    c = ctx(request)
    status = c.control.lifecycle_status(egress_default=c.config.egress.mode_default,
                                        tier_default=c.config.policy.hermes.capability_tier_default)  # fmt: skip
    return {"control": c.control.view().model_dump(), **status, "telegram": c.poll_stats.as_dict()}


@router.post("/control/drain")
async def drain(request: Request, body: DrainRequest, caller: Annotated[Caller, Depends(require(OP))]) -> ControlView:
    return ctx(request).control.drain(caller.actor, body.reason, body.timeout_s, body.scope)


@router.post("/control/down")
async def down(request: Request, body: DownRequest, caller: Annotated[Caller, Depends(require(OP))]) -> ControlView:
    return ctx(request).control.down(caller.actor, body.finished, body.interrupted, body.scope)


@router.post("/control/up")
async def up(request: Request, body: UpRequest, caller: Annotated[Caller, Depends(require(OP))]) -> ControlView:
    """Refused (409 conflict) while killed: kill overrides lifecycle and needs the human resume."""
    return ctx(request).control.up(caller.actor, body.resumed)


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
    c.reissue_cards(c.control.set_dry_run(body.enabled, caller.actor), caller.actor)
    if not body.enabled:
        c.operator_fyi("DRY-RUN OFF", caller.peer.uid, caller.peer.pid)
    return c.control.view()


@router.get("/control/egress-mode")
async def get_egress_mode(request: Request, caller: Annotated[Caller, Depends(require())]) -> EgressModeView:
    c = ctx(request)
    default = c.config.egress.mode_default
    return EgressModeView(mode=c.control.egress_mode(default), default=default)


@router.post("/control/egress-mode")
async def set_egress_mode(
    request: Request, body: EgressModeRequest, caller: Annotated[Caller, Depends(require(OP))]
) -> EgressModeView:
    """§11.5: ossctl asks for the TTY confirm first; root re-renders squid from the flag."""
    c = ctx(request)
    default = c.config.egress.mode_default
    previous = c.control.set_egress_mode(body.mode, caller.actor, default)  # audits and FYIs
    return EgressModeView(mode=c.control.egress_mode(default), default=default, previous=previous)


@router.get("/control/hermes-tier")
async def get_hermes_tier(request: Request, caller: Annotated[Caller, Depends(require())]) -> HermesTierView:
    c = ctx(request)
    default = c.config.policy.hermes.capability_tier_default
    tier = c.control.hermes_tier(default)
    return HermesTierView(tier=tier, default=default, toolsets=list(allowed_toolsets(tier)))


@router.post("/control/hermes-tier")
async def set_hermes_tier(
    request: Request, body: HermesTierRequest, caller: Annotated[Caller, Depends(require(OP))]
) -> HermesTierView:
    """§15.5: ossctl asks for the TTY confirm first; applying the tier to Hermes is PLAN item 36."""
    c = ctx(request)
    default = c.config.policy.hermes.capability_tier_default
    previous, invalidated = c.control.set_hermes_tier(body.tier, caller.actor, default)  # audits, FYIs
    tier = c.control.hermes_tier(default)
    return HermesTierView(tier=tier, default=default, toolsets=list(allowed_toolsets(tier)), previous=previous,
                          invalidated_skill_approvals=len(invalidated))  # fmt: skip
