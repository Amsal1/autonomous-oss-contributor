"""User-directed work (CR-004 Amendment B, DESIGN §9.14, §14.11.19): ``POST /v1/work``.

Operator role (``ossctl work``); Telegram ``/work`` and link cards call the same service in item 47.
Only GitHub issue, PR or repo URLs; the item is marked 👤 and skips ranking, caps, the size
preference and the filters, but not forbidden paths, the AI ban, CLA, one-open-PR or the ceiling.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from ossbot.common.schemas import DiscoverRequest, WorkRequest
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require

router = APIRouter(prefix="/v1")


@router.post("/discover", status_code=202)
async def discover(
    request: Request, body: DiscoverRequest, caller: Annotated[Caller, Depends(require(Role.OPERATOR))]
) -> dict[str, Any]:
    """``ossctl discover [repo...]``: one discovery cycle on the runner's next tick (§9.4)."""
    return ctx(request).contributions.request_discover(body.repos, caller.actor)


@router.post("/discover/claim")
async def claim_discover(request: Request, caller: Annotated[Caller, Depends(require(Role.WORKER))]) -> dict[str, Any]:
    """Runner: consume a pending discover request."""
    return ctx(request).contributions.claim_discover(caller.actor)


@router.post("/work", status_code=201)
async def work(request: Request, body: WorkRequest, caller: Annotated[Caller, Depends(require(Role.OPERATOR))]) -> Any:
    c = ctx(request)
    created = c.contributions.create_user(body.url, body.notes, caller.actor)
    where = created["url"].removeprefix("https://github.com/")
    c.notify(f"🛠 Working on it · {where} · 👤 requested by you\nScouting next; the proposal card follows.")
    return created
