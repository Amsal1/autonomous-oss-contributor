"""Worker-requested outbound actions, sandbox probe results, discovery requests (DESIGN §12.2-12.4).

``POST /v1/actions`` only creates ``issue_claim``/``comment`` actions with a pending approval; it
publishes nothing. ``open_pr``/``push_update`` are created only by ``/package`` (M9).
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ossbot.common.clock import to_iso
from ossbot.common.config import resolve_target
from ossbot.common.ids import new_action_id
from ossbot.common.schemas import TERMINAL_STATES, ActionCreate, DiscoveryRequest, ProbeResult, State
from ossbot.gatekeeper import db, policy
from ossbot.gatekeeper.anomaly import Signal
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.approvals import normalize_text
from ossbot.gatekeeper.authz import Caller, Role, require

router = APIRouter(prefix="/v1")
ISSUE_URL_RE = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)$")


@router.post("/actions", status_code=201)
async def create_action(
    request: Request, body: ActionCreate, caller: Annotated[Caller, Depends(require(Role.WORKER))]
) -> Any:
    c = ctx(request)
    c.control.require_running("request outbound action")
    row = db.one(c.conn, "SELECT * FROM contributions WHERE id = ?", (body.cid,))
    if row is None:
        raise HTTPException(status_code=404, detail="contribution not found")
    if State(row["state"]) in TERMINAL_STATES:
        return JSONResponse(status_code=409, content={"error": "terminal_contribution"})
    allowed_targets = {row["issue_number"]} if body.kind == "issue_claim" else {row["issue_number"], row["pr_number"]}
    if body.target_number not in allowed_targets - {None}:
        return JSONResponse(status_code=400, content={"error": "bad_target"})
    action_id = new_action_id()
    text = normalize_text(body.body).rstrip("\n") + f"\n\n<!-- ossbot:action={action_id} -->"
    violations = policy.check_text(
        "comment", text, c.config, policy.effective_confidentiality(c.conn, c.config), c.kiro_key_sha256
    )
    if violations:
        if any(v.code in {"secret_pattern", "kiro_key"} for v in violations):
            c.anomaly.report(Signal.SECRET_IN_TEXT, f"{body.kind} for {body.cid}", body.cid)
        return JSONResponse(status_code=422, content={"error": "policy", "violations": [v.code for v in violations]})
    with db.transaction(c.conn):
        c.conn.execute(
            "INSERT INTO outbound_actions (id, cid, kind, payload_json, status, created_at) VALUES (?,?,?,?,?,?)",
            (action_id, body.cid, body.kind, json.dumps({"target_number": body.target_number, "body": text}),
             "pending_approval", to_iso(c.clock.now())),
        )  # fmt: skip
        c.audit.append(caller.actor, "action_requested", {"action_id": action_id, "kind": body.kind}, body.cid)
        approval_id = c.approvals.create(
            action_id, tg_chat_id=c.telegram_chat_id, body_parts_total=1, actor=caller.actor
        )
    c.on_approval_created(approval_id)
    return {"action_id": action_id, "approval_id": approval_id}


@router.post("/sandbox/probe-result", status_code=204)
async def probe_result(
    request: Request, body: ProbeResult, caller: Annotated[Caller, Depends(require(Role.SANDBOX))]
) -> None:
    c = ctx(request)
    with db.transaction(c.conn):
        c.audit.append(caller.actor, "safety_probe_result", body.model_dump())


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
