"""FastAPI application for the gatekeeper UDS API (DESIGN §12.2).

Every endpoint is ``async def`` so all SQLite work runs on the event-loop thread, one request at
a time; transactions never interleave on the shared connection.
Errors: 400 schema, 403 role/actor, 404, 409 conflict, 423 paused/killed, 429 rate limit, 500.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, Protocol

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ossbot.common.config import ConfigError
from ossbot.common.control_flags import Blocked
from ossbot.gatekeeper import db
from ossbot.gatekeeper.api import (
    actions,
    audit,
    config,
    contributions,
    control,
    models,
    notify,
    prefs,
    stage_runs,
    work,
)
from ossbot.gatekeeper.approvals import ApprovalError
from ossbot.gatekeeper.authz import PEER_SCOPE_KEY, Forbidden, Peer
from ossbot.gatekeeper.config_snapshot import ReloadConflict
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.contributions import ContributionRefused
from ossbot.gatekeeper.control import ControlConflict
from ossbot.gatekeeper.discovery_prefs import PrefsRefused
from ossbot.gatekeeper.models_admin import ModelSetError
from ossbot.gatekeeper.packaging import prune_retention
from ossbot.gatekeeper.proposals import ProposalRefused, ProposalService
from ossbot.gatekeeper.stage_runs import StageRunRefused
from ossbot.gatekeeper.state_machine import TransitionError

log = logging.getLogger(__name__)
HOUSEKEEPING_SECONDS = 10.0


def _error(status: int, code: str, detail: Any = None, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"error": code}
    if detail is not None:
        body["detail"] = detail
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def _uid(request: Request) -> int | None:
    peer = request.scope.get(PEER_SCOPE_KEY)
    return peer.uid if isinstance(peer, Peer) else None


def _install_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _schema(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e.get("loc", ())), "msg": str(e.get("msg", ""))} for e in exc.errors()]
        return _error(400, "schema_error", errors)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden) -> JSONResponse:
        ctx: AppContext = request.app.state.ctx
        log.warning("forbidden call %s %s by uid %s", request.method, request.url.path, exc.uid,
                    extra={"event": "forbidden_call"})  # fmt: skip
        with db.transaction(ctx.conn):
            ctx.audit.append(f"uid:{exc.uid}", "forbidden_call", {"method": request.method, "path": request.url.path})
        return _error(403, "forbidden", exc.detail)

    @app.exception_handler(TransitionError)
    async def _transition(request: Request, exc: TransitionError) -> JSONResponse:
        if exc.status == 403:
            log.warning(
                "forbidden transition by uid %s: %s",
                _uid(request),
                exc,
                extra={"event": "forbidden_transition"},
            )
        return _error(exc.status, exc.code, str(exc))

    @app.exception_handler(Blocked)
    async def _blocked(request: Request, exc: Blocked) -> JSONResponse:
        return _error(423, exc.flags.state, str(exc), state=exc.flags.state)

    @app.exception_handler(ContributionRefused)
    async def _refused(request: Request, exc: ContributionRefused) -> JSONResponse:
        return _error(exc.status, exc.code)

    @app.exception_handler(ApprovalError)
    async def _approval(request: Request, exc: ApprovalError) -> JSONResponse:
        return _error(409, exc.code, str(exc))

    @app.exception_handler(ConfigError)
    async def _config(request: Request, exc: ConfigError) -> JSONResponse:
        return _error(400, "config_invalid", exc.errors)

    @app.exception_handler(ModelSetError)
    async def _model(request: Request, exc: ModelSetError) -> JSONResponse:
        return _error(400, "model_invalid", str(exc))

    @app.exception_handler(ProposalRefused)
    @app.exception_handler(PrefsRefused)
    @app.exception_handler(StageRunRefused)
    async def _cr004(request: Request, exc: Exception) -> JSONResponse:
        return _error(getattr(exc, "status", 400), getattr(exc, "code", "refused"), getattr(exc, "detail", ""))

    @app.exception_handler(ReloadConflict)
    @app.exception_handler(ControlConflict)
    async def _conflict(request: Request, exc: Exception) -> JSONResponse:
        return _error(409, "conflict", str(exc))

    @app.exception_handler(Exception)
    async def _internal(request: Request, exc: Exception) -> JSONResponse:
        ctx: AppContext = request.app.state.ctx
        log.error("internal error on %s %s", request.method, request.url.path, exc_info=exc,
                  extra={"event": "internal_error"})  # fmt: skip
        try:
            with db.transaction(ctx.conn):
                ctx.audit.append("system", "internal_error", {"path": request.url.path, "type": type(exc).__name__})
        except Exception:
            log.exception("could not audit internal error")
        return _error(500, "internal_error")


async def housekeeping_once(ctx: AppContext) -> None:
    ctx.control.reconcile()
    for svc in ctx.decisions.all():  # delta 9: expiry on all three services; delta 2: timed-out Confirms
        svc.expire_due()
        svc.disarm_expired()
    ctx.state_machine.sweep_expired()
    ProposalService(ctx).sweep_expired()  # CR-004: PROPOSED past proposal.ttl_hours -> PROPOSAL_EXPIRED
    prune_retention(ctx)
    # §15.5: skills Hermes staged (root hasher's pending.json) -> skill_reviews + skill cards; root decisions back
    if ctx.skill_reviews is not None:
        try:
            ctx.skill_reviews.sync()
        except Exception:
            log.exception("skill review import failed", extra={"event": "skill_sync_failed"})
    # delta 8: CardPublisher.recover (no-op until the Telegram front-end starts). Last and guarded, so
    # one card that fails to render cannot stop the expiry sweep on every tick.
    try:
        ctx.card_recover()
    except Exception:
        log.exception("card recovery failed", extra={"event": "card_recover_failed"})


class Service(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


def create_app(ctx: AppContext, *, housekeeping: bool = True, services: Sequence[Service] = ()) -> FastAPI:
    """``services`` start in order inside the lifespan (restart reconciliation first, then the
    Telegram front-end, so no callback can execute before every ``executing`` row is resolved)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        for service in services:
            await service.start()
        task: asyncio.Task[None] | None = None
        if housekeeping:

            async def loop() -> None:
                while True:
                    await asyncio.sleep(HOUSEKEEPING_SECONDS)
                    try:
                        await housekeeping_once(ctx)
                    except Exception:
                        log.exception("housekeeping failed", extra={"event": "housekeeping_failed"})

            task = asyncio.create_task(loop())
        yield
        if task is not None:
            task.cancel()
        for service in reversed(services):
            try:
                await service.stop()
            except Exception:
                log.exception("service stop failed", extra={"event": "service_stop_failed"})

    app = FastAPI(title="ossbot gatekeeper", version="1", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)  # fmt: skip
    app.state.ctx = ctx
    _install_handlers(app)
    for module in (control, config, contributions, actions, models, audit, notify, work, prefs, stage_runs):
        app.include_router(module.router)
    return app
