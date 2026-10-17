"""Owner notifications from worker/requester (rate-limited to 10/h per caller, DESIGN §12.2).

The text is relayed as plain text (no parse mode, no buttons); it never carries control.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ossbot.common.clock import to_iso
from ossbot.common.schemas import NotifyRequest
from ossbot.gatekeeper import db
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require

router = APIRouter(prefix="/v1")
NOTIFY_PER_HOUR = 10


@router.post("/notify", status_code=202)
async def notify(
    request: Request,
    body: NotifyRequest,
    caller: Annotated[Caller, Depends(require(Role.WORKER, Role.REQUESTER))],
) -> Any:
    c = ctx(request)
    since = to_iso(c.clock.now() - timedelta(hours=1))
    if c.audit.count_since("notify", since, caller.actor) >= NOTIFY_PER_HOUR:
        return JSONResponse(status_code=429, content={"error": "rate_limited", "limit_per_hour": NOTIFY_PER_HOUR})
    with db.transaction(c.conn):
        c.audit.append(caller.actor, "notify", {"chars": len(body.text)})
    prefix = "🤖 " if caller.role is Role.REQUESTER else "ℹ️ "
    c.notify(prefix + body.text)
    return {"queued": True}
