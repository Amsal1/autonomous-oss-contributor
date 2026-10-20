"""Worker-requested outbound actions, sandbox probe results, discovery requests (DESIGN §12.2-12.4).

``POST /v1/actions`` only creates ``issue_claim``/``comment`` actions with a pending approval; it
publishes nothing. ``open_pr``/``push_update`` are created only by ``/package`` (M9).
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ossbot.common.config import resolve_target
from ossbot.common.schemas import ActionCreate, DiscoveryRequest, ProbeResult
from ossbot.gatekeeper import db
from ossbot.gatekeeper.action_requests import ActionRefused, request_action
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require

router = APIRouter(prefix="/v1")
ISSUE_URL_RE = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)$")


@router.post("/actions", status_code=201)
async def create_action(
    request: Request, body: ActionCreate, caller: Annotated[Caller, Depends(require(Role.WORKER))]
) -> Any:
    c = ctx(request)
    try:
        done = request_action(c, body.cid, body.kind, body.target_number, body.body, caller.actor)
    except ActionRefused as exc:
        if exc.status == 404:
            raise HTTPException(status_code=404, detail="contribution not found") from exc
        content: dict[str, Any] = {"error": exc.code}
        if exc.violations:
            content["violations"] = exc.violations
        return JSONResponse(status_code=exc.status, content=content)
    if done.approval_id is not None:
        c.on_approval_created(done.approval_id)
    return {"action_id": done.action_id, "approval_id": done.approval_id, "reused": done.reused}


@router.post("/sandbox/probe-result", status_code=204)
async def probe_result(
    request: Request, body: ProbeResult, caller: Annotated[Caller, Depends(require(Role.SANDBOX))]
) -> None:
    c = ctx(request)
    with db.transaction(c.conn):
        c.audit.append(caller.actor, "safety_probe_result", body.model_dump())
    if body.verdict == "inconclusive":  # P1, not a kill: the gate stays closed until a real pass
        c.notify(f"🚨 P1 safety probe {body.probe} inconclusive: the model refused or made no tool call"
                 f" ({body.attempts} tries). Kiro jobs stay blocked until the probes pass; nothing was"
                 " killed. Re-run: ossctl doctor --probes")  # fmt: skip


@router.post("/discovery-requests", status_code=201)
async def discovery_request(
    request: Request,
    body: DiscoveryRequest,
    caller: Annotated[Caller, Depends(require(Role.REQUESTER, Role.OPERATOR))],
) -> Any:
    c = ctx(request)
    match = ISSUE_URL_RE.match(body.url)
    if match is None:
        raise HTTPException(status_code=400, detail="not an issue URL")
    repo, number = match.group(1), int(match.group(2))
    target = resolve_target(c.config.targets, repo)
    if target is None or not target.enabled or not target.discovery:
        return JSONResponse(status_code=400, content={"error": "not_an_enabled_target"})
    cid = c.contributions.create(repo, number, "hermes_request", None, caller.actor)
    return {"cid": cid}
