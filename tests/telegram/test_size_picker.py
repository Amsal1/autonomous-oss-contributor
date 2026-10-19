"""``/size`` (DESIGN §14.14.4, mockup 11; AC 41, U13): xs…xl only (never xxl), per-repo overrides with
``inherit``, immediate and undoable for 60 s, the ceiling shown read-only and never settable."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ossbot.common.clock import FakeClock
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import size_pickers
from ossbot.gatekeeper.telegram.ui import classic
from ossbot.gatekeeper.telegram.ui.limits import label_len
from tests.telegram.reviewkit import labels, sent_with, text, toast, token
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


async def test_size_view_and_class_tap(rig: TgRig) -> None:
    await rig.say("/size")
    mid = sent_with(rig, "Size preference")
    body = text(rig, mid)
    c = rig.ctx.config.policy.diff
    assert "📏 <b>Size preference</b> · autonomous discovery only" in body
    assert f"Ceiling (config only): {c.max_files} files · {c.max_changed_lines:,} code" in body
    assert "👤 Your /work requests ignore this." in body
    found = labels(rig, mid)
    assert not any("xxl" in label for label in found)
    assert found[:5] == ["xs ≤20", "✅ s ≤80", "m ≤250", "l ≤500", "xl ≤1000"]
    await rig.tap(token(rig, mid, "m ≤250"), message=mid)
    assert toast(rig) == "Auto size ≤ m. Proposals already sent keep their size."
    assert rig.ctx.discovery_prefs.effective().autonomous_max_class == "m"
    assert "✅ m ≤250" in labels(rig, mid)
    events = [r[0] for r in rig.ctx.conn.execute("SELECT event FROM audit_events WHERE event LIKE 'discovery_pref%'")]
    assert events  # audited
    await rig.tap(token(rig, mid, "↩️ Undo"), message=mid)
    assert toast(rig) == "↩️ Restored."
    assert rig.ctx.discovery_prefs.effective().autonomous_max_class == "s"


async def test_undo_after_60_s_is_too_late(rig: TgRig, clock: FakeClock) -> None:
    await rig.say("/size")
    mid = sent_with(rig, "Size preference")
    await rig.tap(token(rig, mid, "xs ≤20"), message=mid)
    undo = token(rig, mid, "↩️ Undo")
    clock.advance(seconds=61)
    await rig.tap(undo, message=mid)
    assert toast(rig).startswith("Too late to undo")
    assert rig.ctx.discovery_prefs.effective().autonomous_max_class == "xs"


async def test_typed_size_and_xxl(rig: TgRig) -> None:
    await rig.say("/size xxl")
    assert size_pickers.XXL in rig.last_text().replace("&#x27;", "'")
    await rig.say("/size huge")
    assert "Usage: /size" in rig.last_text()
    await rig.say("/size l")
    assert rig.ctx.discovery_prefs.effective().autonomous_max_class == "l"


async def test_preferred_toggles_keep_one(rig: TgRig) -> None:
    await rig.say("/size")
    mid = sent_with(rig, "Size preference")
    await rig.tap(token(rig, mid, "⭐ Preferred…"), message=mid)
    on = [label for label in labels(rig, mid) if label.startswith("✅")]
    for label in on[:-1]:
        await rig.tap(token(rig, mid, label), message=mid)
    last = [label for label in labels(rig, mid) if label.startswith("✅")]
    assert len(last) == 1
    await rig.tap(token(rig, mid, last[0]), message=mid)
    assert toast(rig) == "Keep at least one preferred class"


async def test_per_repo_override_and_inherit(rig: TgRig) -> None:
    await rig.say("/size")
    mid = sent_with(rig, "Size preference")
    await rig.tap(token(rig, mid, "🏷 Per repo…"), message=mid)
    repo = next(label for label in labels(rig, mid) if "/" in label)
    assert label_len(repo) <= 24
    await rig.tap(token(rig, mid, repo), message=mid)
    assert labels(rig, mid)[0].startswith("Inherit (s)")
    await rig.tap(token(rig, mid, "xs"), message=mid)
    full = next(r for r in size_pickers.enabled_targets(rig.ctx) if r.startswith(repo.replace("…", "")[:5]))
    assert rig.ctx.discovery_prefs.effective(full).autonomous_max_class == "xs"
    assert rig.ctx.discovery_prefs.effective().autonomous_max_class == "s"  # global unchanged
    await rig.tap(token(rig, mid, "Inherit"), message=mid)
    assert rig.ctx.discovery_prefs.effective(full).autonomous_max_class == "s"


def test_view_budgets(ctx: AppContext) -> None:
    for view in ("main", "preferred", "repos"):
        v = size_pickers.size_view(ctx, view, undo="u_x")
        assert len(v.rows) <= 6
        assert len(classic.render(v).text) <= 600 + 200  # picker text stays short
        for row in v.rows:
            for b in row:
                assert label_len(b.label) <= 24
    # U13: no size purpose names the ceiling
    assert not any("ceiling" in p or "diff" in p for p in ("size_set", "filters_set"))
