"""ossbot plugin for Hermes: registers the ``ossbot`` toolset (DESIGN §15.1)."""

from __future__ import annotations

from typing import Any

from .tools import TOOLS


def register(ctx: Any) -> None:
    for schema, handler in TOOLS:
        ctx.register_tool(name=schema["name"], toolset="ossbot", schema=schema, handler=handler)
