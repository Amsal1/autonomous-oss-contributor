"""Audit chain verification against the root-written anchors (DESIGN §12.7, AC 16)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from ossbot.gatekeeper.anchors import parse_anchors
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.audit import verify
from ossbot.gatekeeper.authz import Caller, Role, require

router = APIRouter(prefix="/v1")


@router.get("/audit/verify")
async def audit_verify(request: Request, caller: Annotated[Caller, Depends(require(Role.OPERATOR))]) -> dict[str, Any]:
    c = ctx(request)
    anchors, anchor_errors = parse_anchors(c.paths.audit_anchors_log)
    result = verify(c.conn, anchors)
    errors = [*anchor_errors, *result.errors]
    return {
        "ok": result.ok and not anchor_errors,
        "rows": result.rows,
        "head": {"seq": result.head.seq, "row_hash": result.head.row_hash},
        "anchors_checked": result.anchors_checked,
        "errors": errors,
    }
