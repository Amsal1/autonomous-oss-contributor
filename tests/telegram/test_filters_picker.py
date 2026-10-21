"""``/filters`` (DESIGN §14.14.4, mockup 11; AC 41, AC 50): five pages that wrap around, ≤ 5 toggle rows
plus the pager, labels within §14.17.2, each toggle immediate and undoable, autonomous discovery only.
Page 5 "Open" is post-v1 open discovery (design §3.2)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest

from ossbot.common.schemas import PrefsUpdate
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import size_pickers
from ossbot.gatekeeper.telegram.ui import blocks, classic, rich
from ossbot.gatekeeper.telegram.ui.limits import label_len
from tests.telegram import golden
from tests.telegram.reviewkit import labels, last_sent_id, latest_send, sent_with, text, toast, token
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


@pytest.mark.parametrize("page", [1, 2, 3, 4, 5])
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
    assert "1/5 Kinds" in text(rig, mid)
    assert "✅ Bug" in labels(rig, mid) and "⬜ Breaking change" in labels(rig, mid)
    await rig.tap(token(rig, mid, "◀ Open"), message=mid)
    assert "5/5 Open" in text(rig, mid)
    await rig.tap(token(rig, mid, "Kinds ▶"), message=mid)
    assert "1/5 Kinds" in text(rig, mid)
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


# ----------------------------------------------------------------------- page 5 "Open" (design §3.2)
def _open(ctx: AppContext) -> size_pickers.OpenPrefs:
    found = ctx.discovery_prefs.effective().open
    assert found is not None
    return found


def _check_limits(view: object) -> None:
    rows = view.rows  # type: ignore[attr-defined]
    assert len(rows) <= 6
    for row in rows:
        assert len(row) <= 3
        for b in row:
            assert label_len(b.label) <= {1: 64, 2: 24}.get(len(row), 12), b.label


@pytest.mark.parametrize("view", ["main", "topics", "langs"])
def test_open_views_classic_and_rich(ctx: AppContext, view: str) -> None:
    """Goldens of the three page-5 views (classic text and rich blocks) plus their keyboards."""
    shown = size_pickers.filters_view(ctx, 5, view=view)
    _check_limits(shown)
    _check_limits(size_pickers.filters_view(ctx, 5, view=view, undo="u_x", sub=2))
    keyboard = "\n".join(" | ".join(row) for row in shown.keyboard_labels())
    golden.check(f"filters_open_{view}.html", classic.render(shown).text + "\n--\n" + keyboard + "\n")
    doc = blocks.dump(rich.render(replace(shown, rich=True)))
    golden.check(f"filters_open_{view}.json", json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n")


def test_open_main_shows_the_counts_read_only(ctx: AppContext) -> None:
    body = classic.render(size_pickers.filters_view(ctx, 5)).text
    p = ctx.config.policy.discovery.open
    assert "5/5 Open" in body and "repos found by topic search, not only targets.yaml" in body
    assert f"★ ≥ {p.min_stars} · pushed ≤ {p.pushed_within_days} d · {p.new_repos_per_cycle} repos/cycle" in body
    assert "(config only)" in body
    view = size_pickers.filters_view(ctx, 5)
    purposes = {b.purpose for row in view.rows for b in row}
    assert purposes == {"filters_set"}
    # no button or typed key reaches a count, a budget or the ceiling
    keys = {b.arg.get("key") for row in view.rows for b in row} - {None}
    assert keys == {"open"}
    for key in ("open_min_stars", "open_search_budget", "min_stars", "max_files", "open_topics"):
        assert size_pickers.filter_change(ctx, key, "1") is None


async def test_pager_wraps_over_five_pages(rig: TgRig) -> None:
    await rig.say("/filters")
    mid = sent_with(rig, "Discovery filters")
    assert "1/5 Kinds" in text(rig, mid)
    await rig.tap(token(rig, mid, "◀ Open"), message=mid)
    assert "5/5 Open" in text(rig, mid)
    await rig.tap(token(rig, mid, "Kinds ▶"), message=mid)
    assert "1/5 Kinds" in text(rig, mid)
    await rig.tap(token(rig, mid, "◀ Open"), message=mid)
    await rig.tap(token(rig, mid, "◀ PRs"), message=mid)
    assert "4/5 PRs" in text(rig, mid)


async def _page5(rig: TgRig) -> int:
    await rig.say("/filters")
    mid = sent_with(rig, "Discovery filters")
    await rig.tap(token(rig, mid, "◀ Open"), message=mid)
    return mid


async def test_open_toggle_applies_and_undoes(rig: TgRig) -> None:
    mid = await _page5(rig)
    assert _open(rig.ctx).enabled is True
    await rig.tap(token(rig, mid, "✅ On"), message=mid)
    assert toast(rig).startswith("Saved")
    assert _open(rig.ctx).enabled is False and "⬜ Off" in labels(rig, mid)
    assert rig.ctx.discovery_prefs.effective().sources["open"] == "prefs *"
    await rig.tap(token(rig, mid, "↩️ Undo"), message=mid)
    assert _open(rig.ctx).enabled is True and "✅ On" in labels(rig, mid)


async def test_topics_toggle_and_last_topic_refusal(rig: TgRig) -> None:
    rig.ctx.discovery_prefs.update(PrefsUpdate(filters={"open_topics": ["gitops"]}), "tg:4242")
    mid = await _page5(rig)
    await rig.tap(token(rig, mid, "🏷 Topics (1)…"), message=mid)
    assert "🏷 Topics: 1 on" in text(rig, mid)
    assert "✅ gitops" in labels(rig, mid) and "⬜ devops" in labels(rig, mid)
    await rig.tap(token(rig, mid, "✅ gitops"), message=mid)
    assert toast(rig) == size_pickers.LAST_TOPIC
    assert _open(rig.ctx).topics == ["gitops"]
    await rig.tap(token(rig, mid, "⬜ devops"), message=mid)
    assert _open(rig.ctx).topics == ["gitops", "devops"]
    await rig.tap(token(rig, mid, "✅ gitops"), message=mid)
    assert _open(rig.ctx).topics == ["devops"]
    await rig.tap(token(rig, mid, "↩️ Undo"), message=mid)
    assert _open(rig.ctx).topics == ["gitops", "devops"]
    await rig.tap(token(rig, mid, "▶"), message=mid)  # the second page of topics
    assert "· 2/2" in text(rig, mid)
    await rig.tap(token(rig, mid, "↩️ Back"), message=mid)
    assert "5/5 Open" in text(rig, mid) and "🏷 Topics (2)…" in labels(rig, mid)


async def test_languages_are_a_preference(rig: TgRig) -> None:
    mid = await _page5(rig)
    before = list(_open(rig.ctx).languages_preferred)
    await rig.tap(token(rig, mid, f"💬 Languages ({len(before)})…"), message=mid)
    assert "A preference, not a filter: other languages are still found." in text(rig, mid)
    await rig.tap(token(rig, mid, "✅ Go"), message=mid)
    assert "Go" not in _open(rig.ctx).languages_preferred
    await rig.tap(token(rig, mid, "⬜ Go"), message=mid)
    assert sorted(_open(rig.ctx).languages_preferred) == sorted(before)
    assert rig.ctx.discovery_prefs.effective().filters.languages == []  # page 4's filter is untouched


def _prompts(ctx: AppContext) -> list[str]:
    return [str(r[0]) for r in ctx.conn.execute("SELECT kind FROM pending_input ORDER BY created_at")]


async def test_add_topic_through_a_prompt(rig: TgRig) -> None:
    mid = await _page5(rig)
    await rig.tap(token(rig, mid, "🏷 Topics"), message=mid)
    await rig.tap(token(rig, mid, "➕ Add topic…"), message=mid)
    prompt = last_sent_id(rig)
    assert latest_send(rig, prompt)["reply_markup"]["force_reply"] is True
    assert _prompts(rig.ctx) == ["open_topic_add"]
    before = list(_open(rig.ctx).topics)
    await rig.say("Not a <b>topic</b>!", reply_to=prompt)
    assert text(rig, last_sent_id(rig)) == size_pickers.BAD_TOPIC
    assert _open(rig.ctx).topics == before
    assert db.one(rig.ctx.conn, "SELECT consumed_at FROM pending_input")[0] is not None  # type: ignore[index]
    await rig.tap(token(rig, mid, "➕ Add topic…"), message=mid)
    prompt = last_sent_id(rig)
    await rig.say("  ArgoCD  ", reply_to=prompt)
    added = last_sent_id(rig)
    assert "🏷 Added topic: <code>argocd</code>" in text(rig, added)
    assert _open(rig.ctx).topics == [*before, "argocd"]
    assert "✅ argocd" in labels(rig, added)
    row = db.one(rig.ctx.conn, "SELECT COUNT(*) FROM audit_events WHERE event = 'discovery_prefs_changed'")
    assert row is not None and row[0] == 1
    await rig.tap(token(rig, added, "↩️ Undo"), message=added)
    assert _open(rig.ctx).topics == before
