"""``POST /v1/retention/run`` (DESIGN §18.10): the gatekeeper deletes its own old data. Operator only.

Called by root's ``ossctl gc`` (timer or by hand). 409 ``blocked`` while paused or killed. The DB part
(selection, row deletes, the ``retention_run`` audit event) runs on the event-loop thread like every
other SQLite access; measuring and deleting the directories is filesystem-only and runs in a thread.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ossbot.common.control_flags import Blocked
from ossbot.common.schemas import RetentionReport, RetentionRunRequest
from ossbot.gatekeeper import retention
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require

router = APIRouter(prefix="/v1")


@router.post("/retention/run")
async def run(
    request: Request, body: RetentionRunRequest, caller: Annotated[Caller, Depends(require(Role.OPERATOR))]
) -> Any:
    try:
        plan = retention.prepare(ctx(request), body.dry_run, caller.actor)
    except Blocked as exc:
        return JSONResponse(status_code=409, content={"error": "blocked", "state": exc.flags.state})
    sizes = await asyncio.to_thread(retention.measure, plan)
    await asyncio.to_thread(retention.remove, plan)
    report: RetentionReport = retention.report(plan, sizes)
    return report.model_dump()
