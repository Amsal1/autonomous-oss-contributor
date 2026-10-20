"""``/drain /undrain /down /up`` (DESIGN §14.14.1, §14.16, CR-001 5, AC 31, CR3-AC-16, CR3-AC-33): drain is
immediate, the others confirm first; ``/down`` is a timeout picker then a confirm, and only hands a
workers-scope request to the root helper (the gatekeeper stays up); ``/up`` is refused while killed."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from ossbot.common.clock import FakeClock
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.commands.common import CANCELLED
from ossbot.gatekeeper.telegram.scopes import ONLINE, menu_state
from tests.telegram.reviewkit import labels, sent_with, text, token
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def _request(ctx: AppContext) -> dict[str, object] | None:
    path = ctx.paths.lifecycle_request
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


async def test_drain_is_immediate_and_undrain_confirms(rig: TgRig) -> None:
    ctx = rig.ctx
    await rig.say("/drain")
    assert ctx.control.lifecycle().state == "draining"  # type: ignore[union-attr]
    assert menu_state(ctx) == "draining"
    await rig.say("/undrain")
    mid = sent_with(rig, "Undrain:")
    assert labels(rig, mid) == ["✅ Undrain", "↩️ Cancel"]
    assert ctx.control.lifecycle() is not None  # nothing happened before the tap
    await rig.tap(token(rig, mid, "✅ Undrain"), message=mid)
    assert ctx.control.lifecycle() is None
    assert "Undrained" in text(rig, mid) and labels(rig, mid) == []  # edited to the outcome, no buttons


async def test_down_picker_then_confirm_requests_the_workers_down(rig: TgRig) -> None:
    await rig.say("/down")
    mid = sent_with(rig, "Down (workers)")
    assert labels(rig, mid) == ["60 s", "90 s", "120 s"]
    await rig.tap(token(rig, mid, "90 s"), message=mid)
    assert "running stages get 90 s, then re-queue. The gatekeeper stays up so /up works." in text(rig, mid)
    assert labels(rig, mid) == ["✅ Down (workers)", "↩️ Cancel"]
    assert _request(rig.ctx) is None
    await rig.tap(token(rig, mid, "✅ Down (workers)"), message=mid)
    req = _request(rig.ctx)
    assert req is not None and req["action"] == "down" and req["scope"] == "workers" and req["timeout_s"] == 90
    assert "Down (workers) requested" in text(rig, mid)


async def test_cancel_changes_nothing(rig: TgRig) -> None:
    await rig.say("/down 60")
    mid = sent_with(rig, "running stages get 60 s")
    await rig.tap(token(rig, mid, "↩️ Cancel"), message=mid)
    assert text(rig, mid) == CANCELLED and _request(rig.ctx) is None


async def test_up_confirms_and_is_refused_while_killed(rig: TgRig) -> None:
    ctx = rig.ctx
    await rig.say("/up")
    assert rig.last_text() == "Already up."
    ctx.control.down("tg:4242", 0, 0, "workers")
    await rig.say("/up")
    mid = sent_with(rig, "Up: the workers start")
    up = token(rig, mid, "✅ Up")
    ctx.control.kill("test", "tg:4242")
    await rig.tap(up, message=mid)
    assert "Killed" in text(rig, mid) and _request(ctx) is None  # §14.16: older confirms refused while killed
    await rig.say("/up")  # killed: the command itself refuses (killed mode lets only five commands through)
    assert "Killed" in rig.last_text()


async def test_up_request_after_confirm(rig: TgRig) -> None:
    rig.ctx.control.down("tg:4242", 0, 0, "workers")
    await rig.say("/up")
    mid = sent_with(rig, "Up: the workers start")
    await rig.tap(token(rig, mid, "✅ Up"), message=mid)
    req = _request(rig.ctx)
    assert req is not None and req["action"] == "up"


async def test_a_stale_down_produces_only_its_picker(rig: TgRig, clock: FakeClock) -> None:
    """CR3-AC-16: a /down that waited while the laptop was offline shows the picker with its age."""
    date = int(clock.now().timestamp()) - 3600
    await rig.say("/down", date=date)
    mid = sent_with(rig, "Down (workers)")
    assert labels(rig, mid) == ["60 s", "90 s", "120 s"]
    assert "while I was offline" in text(rig, mid) and _request(rig.ctx) is None


async def test_workers_down_keeps_the_bot_online(rig: TgRig) -> None:
    """CR3-AC-33: only a full down sets "💤 Offline since…"."""
    ctx = rig.ctx
    ctx.control.down("tg:4242", 0, 0, "workers")
    await rig.frontend.on_tick()
    descriptions = [p["short_description"] for m, p in rig.req.calls if m == "setMyShortDescription"]
    assert all(d == ONLINE for d in descriptions)
    ctx.control.down("tg:4242", 0, 0, "distro")
    await rig.frontend.on_tick()
    descriptions = [p["short_description"] for m, p in rig.req.calls if m == "setMyShortDescription"]
    assert descriptions[-1].startswith("💤 Offline since")
