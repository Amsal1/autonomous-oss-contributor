"""Audit chain verification against the root-written anchors (DESIGN §12.7, AC 16)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from ossbot.common.schemas import ApiModel
from ossbot.gatekeeper import db
from ossbot.gatekeeper.anchors import parse_anchors
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.audit import verify
from ossbot.gatekeeper.authz import Caller, Role, require

router = APIRouter(prefix="/v1")


class BackupRecord(ApiModel):
    """``ossctl backup`` (root) records each archive (DESIGN §18.8 step 3); no content, only metadata."""

    archive: Annotated[str, Field(pattern=r"^ossbot-\d{8}-\d{6}\.tar\.age$")]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    size: Annotated[int, Field(ge=0)]
    items: Annotated[int, Field(ge=0)]
    includes_secrets: bool
    includes_artifacts: bool
    includes_attachments: bool


@router.post("/audit/backup-created")
async def backup_created(request: Request, body: BackupRecord,
                         caller: Annotated[Caller, Depends(require(Role.OPERATOR))]) -> dict[str, Any]:  # fmt: skip
    c = ctx(request)
    with db.transaction(c.conn):
        head = c.audit.append(caller.actor, "backup_created", body.model_dump())
    return {"recorded": True, "seq": head.seq}


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
