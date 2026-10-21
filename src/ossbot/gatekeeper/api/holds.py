"""Contribution holds (open-discovery design §13.3, ADR-0023).

Setting a hold is a stop request (sandbox, worker; I9); releasing it is a go action (operator, the
``ossctl egress release`` TTY confirm, announced on Telegram). Refusals answer ``{"error": code}``
(``HoldRefused``, app handler) and schema errors 400 ``schema_error``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse

from ossbot.common.schemas import HoldRequest
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require

router = APIRouter(prefix="/v1")
CidPath = Annotated[str, Path(pattern=r"^c_[0-9A-HJKMNP-TV-Z]{10}$")]


@router.post("/contributions/{cid}/hold")
async def hold(
    request: Request, cid: CidPath, body: HoldRequest,
    caller: Annotated[Caller, Depends(require(Role.SANDBOX, Role.WORKER))],
) -> JSONResponse:  # fmt: skip
    found, created = ctx(request).holds.hold(cid, body, caller.actor)
    return JSONResponse(status_code=201 if created else 200, content=found.model_dump(mode="json"))


@router.get("/holds")
async def list_holds(
    request: Request,
    caller: Annotated[Caller, Depends(require(Role.WORKER, Role.OPERATOR))],
    open_only: Annotated[bool, Query(alias="open")] = True,
) -> list[dict[str, Any]]:
    return [v.model_dump(mode="json") for v in ctx(request).holds.open_holds(include_released=not open_only)]


@router.post("/contributions/{cid}/hold/release")
async def release(
    request: Request, cid: CidPath, caller: Annotated[Caller, Depends(require(Role.OPERATOR))]
) -> dict[str, Any]:
    c = ctx(request)
    released = c.holds.release(cid, caller.actor)
    c.operator_fyi(f"RESUME HOLD {cid}", caller.peer.uid, caller.peer.pid)
    return released.model_dump(mode="json")
