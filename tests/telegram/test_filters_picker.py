"""``/filters`` (DESIGN §14.14.4, mockup 11; AC 41, AC 50): four pages that wrap around, ≤ 5 toggle rows
plus the pager, labels within §14.17.2, each toggle immediate and undoable, autonomous discovery only."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import size_pickers
from ossbot.gatekeeper.telegram.ui.limits import label_len
from tests.telegram.reviewkit import labels, sent_with, text, toast, token
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


@pytest.mark.parametrize("page", [1, 2, 3, 4])
def test_page_shape(ctx: AppContext, page: int) -> None:
    view = size_pickers.filters_view(ctx, page, undo="u_x")
    toggles, pager = view.rows[:-1], view.rows[-1]
    assert len(toggles) <= 5
    assert [b.label for b in pager][1] == "↩️ Undo"
    for b in pager:
        assert label_len(b.label) <= 12
    for row in view.rows:
        for b in row:
            limit = {1: 64, 2: 24}.get(len(row), 12)  # §14.17.2: 3 per row only when every label is ≤ 12
            assert label_len(b.label) <= limit, b.label


def test_age_and_activity_labels_name_their_filter(ctx: AppContext) -> None:
    view = size_pickers.filters_view(ctx, 3)
    found = [b.label.removeprefix("✅ ") for row in view.rows[:-1] for b in row]
    assert all(label.startswith(("Age ", "Active ")) for label in found)
    assert {"Age 30 d", "Age any", "Active 30 d", "Active any"} <= set(found)


async def test_pages_wrap_and_toggles_apply(rig: TgRig) -> None:
    await rig.say("/filters")
    mid = sent_with(rig, "Discovery filters")
    assert "1/4 Kinds" in text(rig, mid)
    assert "✅ Bug" in labels(rig, mid) and "⬜ Breaking change" in labels(rig, mid)
    await rig.tap(token(rig, mid, "◀ PRs"), message=mid)
    assert "4/4 PRs" in text(rig, mid)
    await rig.tap(token(rig, mid, "Kinds ▶"), message=mid)
    assert "1/4 Kinds" in text(rig, mid)
    await rig.tap(token(rig, mid, "✅ Docs"), message=mid)
    assert toast(rig).startswith("Saved")
    f = rig.ctx.discovery_prefs.effective().filters
    assert "docs" not in f.allowed_kinds and "docs" in f.excluded_kinds
    await rig.tap(token(rig, mid, "↩️ Undo"), message=mid)
    assert "docs" in rig.ctx.discovery_prefs.effective().filters.allowed_kinds


async def test_labels_tier_age_and_prs(rig: TgRig) -> None:
    await rig.say("/filters")
    mid = sent_with(rig, "Discovery filters")
    await rig.tap(token(rig, mid, "Labels ▶"), message=mid)
    await rig.tap(token(rig, mid, "⬜ good first issue"), message=mid)
    await rig.tap(token(rig, mid, "T0–T2"), message=mid)
    f = rig.ctx.discovery_prefs.effective().filters
    assert f.require_labels_any == ["good first issue"] and f.validation_tier_max == "T2"
    await rig.tap(token(rig, mid, "⬜ Any label"), message=mid)
    assert rig.ctx.discovery_prefs.effective().filters.require_labels_any == []
    await rig.tap(token(rig, mid, "Age ▶"), message=mid)
    await rig.tap(token(rig, mid, "Age 30 d"), message=mid)
    await rig.tap(token(rig, mid, "Active 90 d"), message=mid)
    f = rig.ctx.discovery_prefs.effective().filters
    assert (f.max_issue_age_days, f.min_maintainer_activity_days) == (30, 90)
    await rig.tap(token(rig, mid, "PRs ▶"), message=mid)
    assert "✅ 1 open PR" in labels(rig, mid)
    await rig.tap(token(rig, mid, "2 open PRs"), message=mid)
    assert rig.ctx.discovery_prefs.effective().filters.max_open_prs_by_me_per_repo == 2
    await rig.tap(token(rig, mid, "⬜ Python"), message=mid)
    assert rig.ctx.discovery_prefs.effective().filters.languages == ["Python"]
