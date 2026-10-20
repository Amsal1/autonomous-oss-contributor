"""A fake Telegraph API and the switch that turns articles on (config + confirmed /reload + token)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl

import httpx

from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegraph import Telegraph, TelegraphWriter, make_client
from tests.conftest import fake_scanner

TOKEN = "telegraphfaketoken0123456789abcdef0123"


@dataclass
class FakeTelegraph:
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    gate: asyncio.Event | None = None  # set: requests wait for it (a stop during createPage)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.strip("/")
        form = dict(parse_qsl(request.content.decode("utf-8")))
        self.calls.append((method, form))
        if self.gate is not None:
            await self.gate.wait()
        if method in {"createPage", "editPage"}:
            path = form.get("path") or "Answer-10-20"
            return httpx.Response(200, json={"ok": True, "result": {"path": path, "url": f"https://telegra.ph/{path}"}})
        if method in {"createAccount", "revokeAccessToken"}:
            return httpx.Response(200, json={"ok": True, "result": {"access_token": "new-telegraph-token-0123456789"}})
        return httpx.Response(404, json={"ok": False, "error": "METHOD_NOT_FOUND"})

    def nodes(self, i: int = -1) -> list[Any]:
        doc: list[Any] = json.loads(self.calls[i][1]["content"])
        return doc


def enable(ctx: AppContext, fake: FakeTelegraph, *, on: bool = True) -> None:
    if on:
        tg = ctx.paths.config_dir / "telegram.yaml"
        tg.write_text(tg.read_text().replace("telegraph: {enabled: false", "telegraph: {enabled: true"))
        ctx.snapshots.apply_reload(ctx.snapshots.prepare_reload().hash, "uid:1000/operator")
    ctx.env = {"TELEGRAPH_ACCESS_TOKEN": TOKEN}
    ctx.reset_secret_scanner()
    ctx.gitleaks_scanner = fake_scanner
    client = make_client(transport=httpx.MockTransport(fake.handler))
    ctx.telegraph = Telegraph(ctx, TelegraphWriter(client, TOKEN))


LONG = ("# Terraform state\n\n" + "Terraform keeps state in a file and compares it with the real world. " * 30
        + "\n\n- plan first\n- then apply\n\n```\nterraform plan\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "See [the docs](https://developer.hashicorp.com/terraform) and **bold** `code`.")  # fmt: skip
