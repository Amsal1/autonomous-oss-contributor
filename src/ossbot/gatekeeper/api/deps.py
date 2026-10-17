"""Shared helpers for the API routers."""

from __future__ import annotations

from fastapi import Request

from ossbot.gatekeeper.context import AppContext


def ctx(request: Request) -> AppContext:
    context: AppContext = request.app.state.ctx
    return context
