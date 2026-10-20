"""``POST /v1/reminders`` (DESIGN §14.19 item 3, §14.12.7 U12): Hermes ``ossbot_reminder_create``.

Role ``requester``. The session key must name an active turn (else 409 ``no_active_turn``); the reminder
stores that turn's thread and fires there as P1 (never in Reviews, Control or OSS feed), subject to quiet
hours. ``when`` is an ISO 8601 time with a zone; ``text`` ≤ 500 characters, untrusted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from ossbot.common.schemas import ApiModel
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require

router = APIRouter(prefix="/v1")
SESSION_HEADER = "X-Hermes-Session-Key"


class ReminderRequest(ApiModel):
    when: Annotated[str, Field(min_length=10, max_length=40)]
    text: Annotated[str, Field(min_length=1, max_length=500)]


@router.post("/reminders", status_code=201)
async def create_reminder(
    request: Request,
    body: ReminderRequest,
    caller: Annotated[Caller, Depends(require(Role.REQUESTER))],
) -> Any:
    from ossbot.gatekeeper.telegram import hermes_out, reminders  # noqa: PLC0415 - telegram imports the context

    c = ctx(request)
    try:
        due_at = datetime.fromisoformat(body.when)
    except ValueError:
        return JSONResponse(status_code=422, content={"error": "bad_when"})
    if due_at.tzinfo is None:
        return JSONResponse(status_code=422, content={"error": "when_needs_a_zone"})
    target = hermes_out.resolve(c, request.headers.get(SESSION_HEADER))
    if target is None:
        return JSONResponse(status_code=409, content={"error": "no_active_turn"})
    try:
        rid = reminders.create(c, due_at, body.text, created_by="hermes", thread_id=target.thread_id)
    except reminders.RemindRefused as exc:
        return JSONResponse(status_code=422, content={"error": "refused", "detail": str(exc)})
    return {"id": rid}
