"""Menu button and command scopes (DESIGN §14.10.2, D2, AC 13) and the short description (AC 33)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from ossbot.common.clock import to_iso
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.commands.registry import COMMANDS, KILLED_MENU, MENU_DESCRIPTION_MAX, menu
from ossbot.gatekeeper.telegram.runtime import ui_of
from tests.conftest import write_flag
from tests.telegram.tgharness import TgRig, make_rig

OWNER = 4242


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def _owner_menus(rig: TgRig) -> list[list[str]]:
    return [[c["command"] for c in p["commands"]] for m, p in rig.req.calls
            if m == "setMyCommands" and p["scope"] == {"type": "chat", "chat_id": OWNER}]  # fmt: skip


async def test_startup_sets_default_empty_owner_full_and_menu_button(ctx: AppContext) -> None:
    async for _rig in make_rig(ctx):
        pass
    calls: list[tuple[str, dict[str, Any]]] = []

    async def record(method: str, payload: dict[str, Any]) -> Any:
        calls.append((method, payload))
        return True

    scopes = ui_of(ctx).scopes
    scopes.applied = None
    await scopes.apply(record)
    by = {(m, str(p.get("scope", {}).get("type"))): p for m, p in calls}
    assert by[("setMyCommands", "default")]["commands"] == []
    owner = by[("setMyCommands", "chat")]
    assert owner["scope"] == {"type": "chat", "chat_id": OWNER}
    assert [c["command"] for c in owner["commands"]] == [c["command"] for c in menu("running")]
    assert ("setChatMenuButton", "None") in by
    assert by[("setChatMenuButton", "None")]["menu_button"] == {"type": "commands"}


def test_menu_matches_the_registry() -> None:
    running = [c["command"] for c in menu("running")]
    for name in COMMANDS:
        if name not in {"up", "undrain", "resume"}:
            assert name in running
    for item in menu("running"):
        assert 1 <= len(item["description"]) <= MENU_DESCRIPTION_MAX


def test_killed_menu_is_reduced() -> None:
    killed = [c["command"] for c in menu("killed")]
    assert killed == [n for n in KILLED_MENU if n in COMMANDS]
    assert "settings" not in killed and "kill" not in killed
    assert set(killed) <= {"status", "resume", "logs", "help", "start"}


async def test_menu_follows_the_control_state(ctx: AppContext, rig: TgRig) -> None:
    ctx.control.kill("test", "tg:4242")
    await rig.frontend.on_tick()
    assert _owner_menus(rig)[-1] == [c["command"] for c in menu("killed")]
    await rig.frontend.on_tick()  # no change, no call
    assert len(_owner_menus(rig)) == 1


async def test_short_description_follows_the_named_down_states(ctx: AppContext, rig: TgRig) -> None:
    def descriptions() -> list[str]:
        return [p["short_description"] for m, p in rig.req.calls if m == "setMyShortDescription"]

    now = to_iso(ctx.clock.now())
    write_flag(ctx.paths.lifecycle_flag, {"state": "down", "scope": "workers", "reason": "t", "actor": "tg:1",
                                          "ts": now, "timeout_s": 10})  # fmt: skip
    await rig.frontend.on_tick()
    assert descriptions() == []  # workers down: still "🟢 Online" (AC 33)
    write_flag(ctx.paths.lifecycle_flag, {"state": "down", "scope": "distro", "reason": "t", "actor": "uid:0",
                                          "ts": now, "timeout_s": 10})  # fmt: skip
    await rig.frontend.on_tick()
    assert descriptions()[-1].startswith("💤 Offline since ")
    ctx.paths.lifecycle_flag.unlink()
    await rig.frontend.on_tick()
    assert descriptions()[-1] == "🟢 Online"
