"""Config snapshot read and two-step human-confirmed reload (DESIGN §7.9)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from ossbot.common.schemas import ReloadRequest
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require

router = APIRouter(prefix="/v1")


@router.get("/config/snapshot")
async def snapshot(request: Request, caller: Annotated[Caller, Depends(require())]) -> dict[str, Any]:
    current = ctx(request).snapshots.current
    return {"hash": current.hash, "files": {k: v.decode("utf-8") for k, v in sorted(current.files.items())}}


@router.post("/config/reload")
async def reload(
    request: Request, body: ReloadRequest, caller: Annotated[Caller, Depends(require(Role.OPERATOR))]
) -> dict[str, Any]:
    c = ctx(request)
    if body.confirm_hash is None:
        preview = c.snapshots.prepare_reload()
        return {
            "applied": False,
            "hash": preview.hash,
            "diff": preview.diff,
            "changed_files": list(preview.changed_files),
        }
    snap, invalidated = c.snapshots.apply_reload(body.confirm_hash, caller.actor)
    c.operator_fyi("CONFIG RELOAD", caller.peer.uid, caller.peer.pid)
    return {"applied": True, "hash": snap.hash, "invalidated_approvals": [i.approval_id for i in invalidated]}
