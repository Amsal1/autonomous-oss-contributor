"""In-process API client with a fake SO_PEERCRED peer (scope injection, as the UDS protocol does)."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import FastAPI

from ossbot.gatekeeper.app import create_app
from ossbot.gatekeeper.authz import PEER_SCOPE_KEY, Peer
from ossbot.gatekeeper.context import AppContext


class InjectPeer:
    def __init__(self, app: FastAPI, uid: int | None) -> None:
        self.app = app
        self.uid = uid

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope["type"] == "http" and self.uid is not None:
            scope = dict(scope)
            scope[PEER_SCOPE_KEY] = Peer(pid=os.getpid(), uid=self.uid, gid=self.uid)
        await self.app(scope, receive, send)


def client(ctx: AppContext, uid: int | None, app: FastAPI | None = None) -> httpx.AsyncClient:
    application = app or create_app(ctx, housekeeping=False)
    transport = httpx.ASGITransport(app=InjectPeer(application, uid))  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=transport, base_url="http://gatekeeper")
