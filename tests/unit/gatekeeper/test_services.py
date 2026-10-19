"""Gatekeeper service wiring (PLAN Decision 5): reconciliation first; Telegram only with token and user."""

from __future__ import annotations

import pytest
from telegram.ext import CallbackQueryHandler, MessageHandler, TypeHandler

from ossbot.gatekeeper.app import create_app
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.main import build_services
from ossbot.gatekeeper.reconcile import ReconcileService
from ossbot.gatekeeper.telegram.bot import TelegramFrontend, build_application, telegram_enabled
from ossbot.gatekeeper.telegram.handlers import Handlers
from tests.unit.gatekeeper.api_helpers import client


def test_no_secrets_means_reconcile_only(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_WRITE_TOKEN", raising=False)
    services = build_services(ctx)
    assert [type(s) for s in services] == [ReconcileService]


def test_telegram_enabled_rules(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    assert telegram_enabled(ctx, "123:abc")
    assert not telegram_enabled(ctx, None)
    profile = ctx.paths.config_dir / "profile.yaml"
    profile.write_text(profile.read_text().replace("user_id: 4242", "user_id: 0"))
    ctx.snapshots.apply_reload(ctx.snapshots.prepare_reload().hash, "uid:1000/operator")
    assert not telegram_enabled(ctx, "123:abc")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:NOT-A-REAL-TOKEN")
    assert [type(s) for s in build_services(ctx)] == [ReconcileService]


def test_frontend_built_with_proxy_and_handlers(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:NOT-A-REAL-TOKEN")
    services = build_services(ctx)
    assert [type(s) for s in services] == [ReconcileService, TelegramFrontend]
    application = build_application("123456:NOT-A-REAL-TOKEN", Handlers(ctx))
    kinds = {type(h) for group in application.handlers.values() for h in group}
    assert kinds == {CallbackQueryHandler, MessageHandler, TypeHandler}  # TypeHandler: 10.3 stop updates


async def test_services_start_before_requests_and_stop(ctx: AppContext) -> None:
    order: list[str] = []

    class Probe:
        def __init__(self, name: str) -> None:
            self.name = name

        async def start(self) -> None:
            order.append(f"start {self.name}")

        async def stop(self) -> None:
            order.append(f"stop {self.name}")

    app = create_app(ctx, housekeeping=False, services=[Probe("reconcile"), Probe("telegram")])
    async with app.router.lifespan_context(app), client(ctx, 1000, app=app) as c:
        assert (await c.get("/v1/health")).status_code == 200
    assert order == ["start reconcile", "start telegram", "stop telegram", "stop reconcile"]
