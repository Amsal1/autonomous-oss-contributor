"""Worker-requested outbound actions, sandbox probe results, discovery requests (DESIGN §12.2-12.4).

``POST /v1/actions`` only creates ``issue_claim``/``comment`` actions with a pending approval; it
publishes nothing. ``open_pr``/``push_update`` are created only by ``/package`` (M9).
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ossbot.common.config import REPO_RE, resolve_target
from ossbot.common.schemas import DISCOVERY_URL_RE, ActionCreate, DiscoveryRequest, ProbeResult
from ossbot.gatekeeper import db
from ossbot.gatekeeper.action_requests import ActionRefused, request_action
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require

router = APIRouter(prefix="/v1")
DISCOVERY_URL = re.compile(DISCOVERY_URL_RE)
REPO_SLUG = re.compile(REPO_RE)


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
    """A Hermes suggestion (design §11; untrusted input). Only the slug and the issue number are used or
    stored; nothing here approves anything (both gates still apply to whatever gets created)."""
    c = ctx(request)
    match = DISCOVERY_URL.fullmatch(body.url)
    repo = f"{match['owner']}/{match['repo']}" if match is not None else ""
    if match is None or REPO_SLUG.fullmatch(repo) is None:  # no echo of the input
        detail = [{"loc": ["body", "url"], "msg": "not a repo"}]
        return JSONResponse(status_code=400, content={"error": "schema_error", "detail": detail})
    number = int(match["n"]) if match["n"] else None
    target = resolve_target(c.config.targets, repo)
    if target is not None:  # a config target: unchanged for issues
        if number is None:
            return JSONResponse(status_code=400, content={"error": "already_a_target"})
        if not target.enabled or not target.discovery:
            return JSONResponse(status_code=400, content={"error": "not_an_enabled_target"})
        return {"cid": c.contributions.create(target.repo, number, "hermes_request", None, caller.actor)}
    found = c.discovered.get(repo)
    if found is not None and found.status in ("candidate", "kept"):
        if number is None:
            return JSONResponse(status_code=200, content={"repo": found.repo, "status": found.status})
        limit = c.discovery_prefs.effective(found.repo).filters.max_open_prs_by_me_per_repo
        if c.discovered.open_pr_count(found.repo) >= limit:  # plan D1 (review-3 M2): one open PR per repo
            return JSONResponse(status_code=409, content={"error": "repo_has_open_pr"})
        return {"cid": c.contributions.create(found.repo, number, "hermes_request", None, caller.actor)}
    view = c.discovered.suggest(repo, number, caller.actor)  # never/skipped/denied/limits raise DiscoveredRefused
    return JSONResponse(status_code=202, content={"queued": True, "repo": view.repo, "status": view.status})
