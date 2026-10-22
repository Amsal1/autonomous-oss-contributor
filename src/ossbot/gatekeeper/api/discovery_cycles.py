"""Discovery cycle reports from the runner (discovery fix 5, ADR-0003 addendum 2026-10-22).

``POST /v1/discovery-cycles`` (worker only, ``DiscoveryCycleReport``, extra=forbid, bounded; a schema
error is 400 ``schema_error`` without an echo). It records the report as the audit event
``discovery_cycle`` and, for a finished cycle, queues the silent Discovery FYI
(``telegram/discovery_fyi.py``). It changes no state: no transition, no approval, no control flag. At most
``REPORTS_PER_HOUR`` reports per caller and hour (429 ``rate_limited``).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ossbot.common.clock import to_iso
from ossbot.common.schemas import DiscoveryCycleReport
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require

router = APIRouter(prefix="/v1")
REPORTS_PER_HOUR = 120  # a cut cycle resumes no sooner than 1 min, so a healthy runner stays far below


@router.post("/discovery-cycles")
async def discovery_cycle(
    request: Request, body: DiscoveryCycleReport, caller: Annotated[Caller, Depends(require(Role.WORKER))]
) -> Any:
    from ossbot.gatekeeper.telegram import discovery_fyi  # noqa: PLC0415 - telegram imports the context

    c = ctx(request)
    since = to_iso(c.clock.now() - timedelta(hours=1))
    if c.audit.count_since(discovery_fyi.AUDIT_EVENT, since, caller.actor) >= REPORTS_PER_HOUR:
        return JSONResponse(status_code=429, content={"error": "rate_limited", "limit_per_hour": REPORTS_PER_HOUR})
    discovery_fyi.record(c, body, caller.actor)
    return {"recorded": True}
