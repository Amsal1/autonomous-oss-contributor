"""Owner notifications from worker/requester (rate-limited to 10/h per caller, DESIGN §12.2, §14.12.7).

* **Hermes** (role ``requester``): the plugin sends the ``X-Hermes-Session-Key`` of the turn it serves;
  without an active turn of that session the answer is 409 ``no_active_turn`` and nothing is sent (U12).
  The notice goes to that turn's thread (never Reviews, Control or OSS feed), starts with 🤖, and is P2
  unless ``priority: "P1"`` (at most 5 P1 per local day, then P2).
* **Worker notices** (role ``worker``, the information-sign prefix): no session; 📈 OSS feed as P2, or P1 on request.

``priority`` is ``"P1"`` or ``"P2"``; anything else, ``"P0"`` included, is a 422. The text is relayed
escaped (classic HTML built by ``ui/ids.autocode``: no markup from the caller, ids as one-tap code, no
buttons); it never carries control.
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
SESSION_HEADER = "X-Hermes-Session-Key"
PRIORITIES = frozenset({"P1", "P2"})


@router.post("/notify", status_code=202)
async def notify(
    request: Request,
    body: NotifyRequest,
    caller: Annotated[Caller, Depends(require(Role.WORKER, Role.REQUESTER))],
) -> Any:
    c = ctx(request)
    if body.priority not in PRIORITIES:
        return JSONResponse(status_code=422, content={"error": "bad_priority", "allowed": sorted(PRIORITIES)})
    since = to_iso(c.clock.now() - timedelta(hours=1))
    if c.audit.count_since("notify", since, caller.actor) >= NOTIFY_PER_HOUR:
        return JSONResponse(status_code=429, content={"error": "rate_limited", "limit_per_hour": NOTIFY_PER_HOUR})
    if caller.role is Role.REQUESTER:
        from ossbot.gatekeeper.telegram import hermes_out  # noqa: PLC0415 - telegram imports the context

        target = hermes_out.resolve(c, request.headers.get(SESSION_HEADER))
        if target is None:
            return JSONResponse(status_code=409, content={"error": "no_active_turn"})
        with db.transaction(c.conn):
            c.audit.append(caller.actor, "notify", {"chars": len(body.text), "priority": body.priority})
        priority = hermes_out.hermes_priority(c, body.priority)
        hermes_out.deliver(c, target, hermes_out.notice_view(body.text), label="notice", priority=priority)
        return {"queued": True, "priority": f"P{priority}"}
    with db.transaction(c.conn):
        c.audit.append(caller.actor, "notify", {"chars": len(body.text), "priority": body.priority})
    priority = 1 if body.priority == "P1" else 2
    c.notify("ℹ️ " + body.text, priority=priority, thread="feed")  # quiet hours still apply (§14.13.2)
    return {"queued": True, "priority": f"P{priority}"}
