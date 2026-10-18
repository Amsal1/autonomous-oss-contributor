"""Discovery preferences (CR-004, DESIGN §9.14): ``GET/PUT /v1/discovery-prefs`` and the 60 s undo.

Written only through ``gatekeeper.discovery_prefs``; ceiling keys are refused there (U13). Read by
the runner (worker) for ranking and filters of autonomous discovery.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from ossbot.common.config import REPO_RE
from ossbot.common.schemas import PrefsUndo, PrefsUpdate
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require
from ossbot.gatekeeper.proposals import ProposalService

router = APIRouter(prefix="/v1")


@router.get("/discovery-prefs")
async def get_prefs(
    request: Request,
    caller: Annotated[Caller, Depends(require(Role.OPERATOR, Role.WORKER))],
    repo: Annotated[str | None, Query(pattern=REPO_RE)] = None,
) -> dict[str, Any]:
    c = ctx(request)
    prefs = c.discovery_prefs
    return {"effective": prefs.effective(repo).as_dict(), "rows": prefs.rows(), "pacing": ProposalService(c).pacing()}


@router.put("/discovery-prefs")
async def put_prefs(
    request: Request, body: PrefsUpdate, caller: Annotated[Caller, Depends(require(Role.OPERATOR))]
) -> dict[str, Any]:
    return ctx(request).discovery_prefs.update(body, caller.actor)


@router.post("/discovery-prefs/undo")
async def undo_prefs(
    request: Request, body: PrefsUndo, caller: Annotated[Caller, Depends(require(Role.OPERATOR))]
) -> dict[str, Any]:
    return ctx(request).discovery_prefs.undo(body.undo_id, caller.actor)
