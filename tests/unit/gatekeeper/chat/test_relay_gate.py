"""The M9 relay gate (DESIGN §15.2, §15.6; PLAN item 37): kill/pause/lifecycle, the probe file younger
than 2 h, and the tier's tool surface (drift soft-kills, unavailable only closes)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from ossbot.common.control_flags import FlagState, HermesTier, KillInfo, PauseInfo
from ossbot.gatekeeper.chat import backend as backend_mod
from ossbot.gatekeeper.chat.backend import REPLIES, RelayGate
from ossbot.gatekeeper.chat.hermes_surface import SurfaceResult
from ossbot.gatekeeper.context import AppContext

CHECKED = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
PASSED: dict[str, Any] = {"passed": True, "checked_at": "2026-10-19T12:00:00Z"}
KILL = KillInfo("soft", "test", "ossadmin", "2026-10-19T12:00:00Z")
PAUSE = PauseInfo("test", "ossadmin", "2026-10-19T12:00:00Z")


class Rig:
    def __init__(self) -> None:
        self.flags = FlagState(None, None)
        self.lifecycle: str | None = None
        self.probe: dict[str, Any] | None = dict(PASSED)
        self.tier: HermesTier = "self_improve"
        self.result = SurfaceResult("ok", "tier self_improve")
        self.now = CHECKED.timestamp() + 60
        self.surface_calls: list[HermesTier] = []
        self.kills: list[SurfaceResult] = []
        self.gate = RelayGate(flags=lambda: self.flags, lifecycle=lambda: self.lifecycle, probe_doc=lambda: self.probe,
                              surface=self._surface, tier=lambda: self.tier, on_surface_kill=self.kills.append,
                              now=lambda: self.now)  # fmt: skip

    def _surface(self, tier: HermesTier) -> SurfaceResult:
        self.surface_calls.append(tier)
        return self.result


@pytest.fixture
def rig() -> Rig:
    return Rig()


async def test_open_when_everything_passes(rig: Rig) -> None:
    result = await rig.gate.check()
    assert result.open and result.reason == "ok" and rig.surface_calls == ["self_improve"]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda r: setattr(r, "flags", FlagState(KILL, None)), "killed"),
        (lambda r: setattr(r, "flags", FlagState(KILL, PAUSE)), "killed"),
        (lambda r: setattr(r, "flags", FlagState(None, PAUSE)), "paused"),
        (lambda r: setattr(r, "lifecycle", "draining"), "workers_down"),
        (lambda r: setattr(r, "lifecycle", "down"), "workers_down"),
        (lambda r: setattr(r, "probe", None), "probe_pending"),
        (lambda r: setattr(r, "probe", {**PASSED, "passed": False}), "probe_pending"),
        (lambda r: setattr(r, "probe", {**PASSED, "checked_at": "yesterday"}), "probe_pending"),
        (lambda r: setattr(r, "now", CHECKED.timestamp() + 2 * 3600), "probe_pending"),  # exactly 2 h: stale
    ],
)
async def test_closed_states_never_ask_hermes(rig: Rig, change: Any, reason: str) -> None:
    change(rig)
    result = await rig.gate.check()
    assert not result.open and result.reason == reason and rig.surface_calls == []
    assert result.reply == REPLIES[reason] and result.reply.startswith("🤖")


async def test_probe_just_under_two_hours_is_fresh(rig: Rig) -> None:
    rig.now = CHECKED.timestamp() + 2 * 3600 - 1
    assert (await rig.gate.check()).open


@pytest.mark.parametrize("outcome", ["drift", "unverifiable"])
async def test_surface_drift_soft_kills(rig: Rig, outcome: str) -> None:
    rig.result = SurfaceResult(outcome, "tier locked", ("terminal",))  # type: ignore[arg-type]
    result = await rig.gate.check()
    assert not result.open and result.reason == "surface" and outcome in result.detail
    assert rig.kills == [rig.result]


async def test_unavailable_hermes_only_closes(rig: Rig) -> None:
    rig.result = SurfaceResult("unavailable", "ConnectError")
    result = await rig.gate.check()
    assert not result.open and result.reason == "hermes_unavailable" and rig.kills == []


async def test_ok_is_cached_per_tier_for_the_ttl(rig: Rig) -> None:
    assert (await rig.gate.check()).open
    rig.now += backend_mod.SURFACE_TTL_S - 1
    assert (await rig.gate.check()).open and len(rig.surface_calls) == 1
    rig.tier = "locked"  # a tier switch is never served from the cache
    assert (await rig.gate.check()).open and rig.surface_calls == ["self_improve", "locked"]
    rig.now += backend_mod.SURFACE_TTL_S
    await rig.gate.check()
    assert len(rig.surface_calls) == 3


async def test_a_failed_check_drops_the_cache(rig: Rig) -> None:
    assert (await rig.gate.check()).open
    rig.gate.invalidate()
    rig.result = SurfaceResult("drift", "tier self_improve", ("terminal",))
    assert not (await rig.gate.check()).open
    rig.result = SurfaceResult("ok")
    assert (await rig.gate.check()).open and len(rig.surface_calls) == 3


async def test_kill_wins_over_a_cached_ok(rig: Rig) -> None:
    assert (await rig.gate.check()).open
    rig.flags = FlagState(KILL, None)
    assert (await rig.gate.check()).reason == "killed"


async def test_context_gate_is_closed_without_a_probe_file(ctx: AppContext) -> None:
    """The wired gate (build_context): no probe result in the test tree, so the relay stays closed."""
    assert ctx.chat is not None
    result = await ctx.chat.gate.check()
    assert not result.open and result.reason == "probe_pending"
