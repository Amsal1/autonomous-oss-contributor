"""Discovered targets (post-v1 open discovery, design §8.4) and target resolution for the runner.

Every write goes through ``gatekeeper.discovered_targets`` (its rules, audit and FYI); refusals answer
``{"error": code}`` (``DiscoveredRefused``, app handler) and schema errors 400 ``schema_error``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from ossbot.common.config import REPO_RE, resolve_target
from ossbot.common.schemas import DiscoveredDecision, DiscoveredFacts, DiscoveredStatus, ResolvedTargetView
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require

router = APIRouter(prefix="/v1")


@router.get("/discovered-targets")
async def list_discovered(
    request: Request,
    caller: Annotated[Caller, Depends(require(Role.WORKER, Role.OPERATOR))],
    status: DiscoveredStatus | None = None,
    untried: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[dict[str, Any]]:
    rows = ctx(request).discovered.list(status, untried=untried, limit=limit)
    return [v.model_dump() for v in rows]


@router.get("/discovered-targets/excluded")
async def excluded(request: Request, caller: Annotated[Caller, Depends(require(Role.WORKER))]) -> dict[str, Any]:
    return {"repos": ctx(request).discovered.excluded()}


@router.post("/discovered-targets")
async def record(
    request: Request, body: DiscoveredFacts, caller: Annotated[Caller, Depends(require(Role.WORKER))]
) -> dict[str, Any]:
    return ctx(request).discovered.record(body, caller.actor).model_dump()


@router.post("/discovered-targets/decision")
async def decision(
    request: Request, body: DiscoveredDecision, caller: Annotated[Caller, Depends(require(Role.OPERATOR))]
) -> dict[str, Any]:
    return ctx(request).discovered.decide(body.repo, body.decision, caller.actor)


@router.get("/targets/resolve")
async def resolve(
    request: Request,
    caller: Annotated[Caller, Depends(require(Role.WORKER))],
    repo: Annotated[str, Query(pattern=REPO_RE)],
) -> Any:
    c = ctx(request)
    configured = resolve_target(c.config.targets, repo)
    target = configured or c.discovered.effective_target(repo)
    if target is None:
        return JSONResponse(status_code=404, content={"error": "not_a_target"})
    return {"source": "config" if configured is not None else "discovered",
            "target": ResolvedTargetView.from_target(target).model_dump()}  # fmt: skip
