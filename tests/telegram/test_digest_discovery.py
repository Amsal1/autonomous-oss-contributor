"""The daily digest's Discovery section (discovery fix 5c and 10): built only from the gatekeeper's own
tables (``discovery_cycle`` audit rows, ``source = 'discover'`` contributions, outcomes and stage runs in
the window): cycles finished and cut by reason, candidates, new items, proposals, the top drop reasons, the
on-main skips (always shown) and discovery error classes; a period without a finished cycle warns."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

from ossbot.common.clock import FakeClock, to_iso
from ossbot.common.schemas import DiscoveryCycleReport, State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import digest, discovery_fyi
from ossbot.gatekeeper.telegram.ui import blocks, classic, rich
from ossbot.gatekeeper.telegram.ui import kit as k
from tests.conftest import insert_contribution
from tests.telegram import golden

WORKER = "uid:2001/worker"


def cycle(ctx: AppContext, outcome: str = "finished", cut_reason: str | None = None, **kw: int) -> None:
    now = to_iso(ctx.clock.now())
    doc = {"outcome": outcome, "scope": "full", "cut_reason": cut_reason, "started_at": now, "ended_at": now, **kw}
    discovery_fyi.record(ctx, DiscoveryCycleReport.model_validate(doc), WORKER)


def dropped(ctx: AppContext, state: State, reason: str, *, source: str = "discover") -> str:
    return insert_contribution(ctx, state, state_reason=reason, source=source)


def seeded(ctx: AppContext, clock: FakeClock) -> dict[str, object]:
    cycle(ctx, candidates=30, open_candidates=4)
    cycle(ctx, candidates=10, open_candidates=0)
    cycle(ctx, "cut", "search_limit")
    cycle(ctx, "cut", "search_limit")
    cycle(ctx, "cut", "core_limit")
    dropped(ctx, State.SKIPPED, "ai_policy_unclear")
    dropped(ctx, State.SKIPPED, "ai_policy_unclear")
    dropped(ctx, State.SKIPPED, "already_fixed_on_main")
    dropped(ctx, State.SKIPPED, "claimed_by_other")
    dropped(ctx, State.SKIPPED, "ai_policy_unclear", source="user")  # not discovery's
    failed = dropped(ctx, State.FAILED, "output_invalid")
    proposed = insert_contribution(ctx, State.PROPOSED)
    now = to_iso(ctx.clock.now())
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO outcomes (cid, upstream, kind, size_class, source, proposed_at, updated_at)"
            " VALUES (?, 'langfuse/langfuse', 'bugfix', 'xs', 'discover', ?, ?)",
            (proposed, now, now),
        )
        ctx.conn.execute(
            "INSERT INTO stage_runs (id, cid, stage, attempt, status, started_at, error_class) VALUES"
            " ('r_0000000001', ?, 'plan', 1, 'failed', ?, 'output_invalid')",
            (failed, now),
        )
    clock.advance(timedelta(minutes=1))
    return digest.collect(ctx, digest.now_period(ctx, "daily"), {})["discovery"]  # type: ignore[no-any-return]


def test_the_discovery_section_from_the_gatekeepers_tables(ctx: AppContext, clock: FakeClock) -> None:
    disc = seeded(ctx, clock)
    assert disc == {
        "finished": 2, "cut": {"search_limit": 2, "core_limit": 1}, "candidates": 44, "created": 6, "proposals": 1,
        "drops": {"ai_policy_unclear": 2, "claimed_by_other": 1, "output_invalid": 1},
        "on_main": {"already_fixed_on_main": 1, "not_reproducible_on_main": 0},
        "errors": {"output_invalid": 1},
    }  # fmt: skip
    view = k.View(kind="digest", blocks=[digest.discovery_block(disc)], rich=True)
    shown = classic.render(view).text
    golden.check("digest_daily_discovery.html", shown + "\n")
    dumped = json.dumps(blocks.dump(rich.render(replace(view, rich=True))), ensure_ascii=False, indent=1,
                        sort_keys=True)  # fmt: skip
    golden.check("digest_daily_discovery.json", dumped + "\n")
    assert "2 finished · 3 cut (search limit ×2 · core limit ×1)" in shown
    assert "🌿 On main: 1 already fixed · 0 not reproducible" in shown
    assert "No discovery cycle finished" not in shown


def test_the_block_follows_the_facts_in_the_digest(ctx: AppContext, clock: FakeClock) -> None:
    period = digest.now_period(ctx, "daily")
    stats = digest.collect(ctx, period, {})
    names = [b.name for b in digest.view(ctx, period, stats, {}).blocks]
    assert names[:2] == ["facts", "discovery"]
    del stats["discovery"]  # an older stats_json row still renders, without the section
    assert "discovery" not in [b.name for b in digest.view(ctx, period, stats, {}).blocks]


def test_no_finished_cycle_shows_the_warning_and_zero_on_main(ctx: AppContext, clock: FakeClock) -> None:
    cycle(ctx, "cut", "stopped")
    clock.advance(timedelta(minutes=1))
    disc = digest.collect(ctx, digest.now_period(ctx, "daily"), {})["discovery"]
    shown = classic.render(k.View(kind="digest", blocks=[digest.discovery_block(disc)])).text
    assert "⚠️ No discovery cycle finished in this period" in shown
    assert "0 already fixed · 0 not reproducible" in shown and "stopped ×1" in shown


def test_reasons_are_untrusted_text(ctx: AppContext, clock: FakeClock) -> None:
    dropped(ctx, State.SKIPPED, "<b>x</b>")
    clock.advance(timedelta(minutes=1))
    disc = digest.collect(ctx, digest.now_period(ctx, "daily"), {})["discovery"]
    shown = classic.render(k.View(kind="digest", blocks=[digest.discovery_block(disc)])).text
    assert "<b>x</b>" not in shown and "&lt;b&gt;x" in shown


def test_rows_outside_the_window_are_not_counted(ctx: AppContext, clock: FakeClock) -> None:
    cycle(ctx, candidates=5)
    dropped(ctx, State.SKIPPED, "already_fixed_on_main")
    clock.advance(timedelta(days=1, minutes=1))
    disc = digest.collect(ctx, digest.now_period(ctx, "daily"), {})["discovery"]
    assert disc["finished"] == 0 and disc["on_main"]["already_fixed_on_main"] == 0 and disc["created"] == 0
