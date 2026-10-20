"""``/hermes tier`` (DESIGN §14.14.1, §15.5; CR2-3a, AC 35): picker, confirm with the toolset diff, then the
tier flag, the skill-card invalidation (delta 9) and a fresh surface probe."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ossbot.gatekeeper.chat.hermes_surface import allowed_toolsets
from ossbot.gatekeeper.context import AppContext
from tests.telegram.reviewkit import labels, latest, sent_with, text, token
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def _tier(ctx: AppContext) -> str:
    return ctx.control.hermes_tier(ctx.config.policy.hermes.capability_tier_default)


async def test_tier_switch_with_toolset_diff_and_probe(rig: TgRig) -> None:
    ctx = rig.ctx
    start = _tier(ctx)
    assert start == "self_improve"
    await rig.say("/hermes tier")
    mid = sent_with(rig, "Hermes tier")
    assert [lbl.split()[0] for lbl in labels(rig, mid)] == ["locked", "research", "self_improve"]
    await rig.tap(token(rig, mid, "research"), message=mid)
    body = text(rig, mid)
    removed = sorted(set(allowed_toolsets("self_improve")) - set(allowed_toolsets("research")))
    assert "Hermes tier self_improve → research" in body
    assert all(f"−{t}" in body for t in removed)
    assert rig.buttons(latest(rig, mid))[0][0]["style"] == "primary"  # narrowing
    assert _tier(ctx) == start
    await rig.tap(token(rig, mid, "✅ Switch to research"), message=mid)
    assert _tier(ctx) == "research"
    assert "surface probe" in text(rig, mid)  # the probe re-ran (no Hermes key here: unavailable)


async def test_widening_tier_is_danger(rig: TgRig) -> None:
    ctx = rig.ctx
    ctx.control.set_hermes_tier("locked", "tg:4242", ctx.config.policy.hermes.capability_tier_default)
    await rig.say("/hermes tier self_improve")
    mid = sent_with(rig, "Hermes tier locked → self_improve")
    assert rig.buttons(latest(rig, mid))[0][0]["style"] == "danger"
