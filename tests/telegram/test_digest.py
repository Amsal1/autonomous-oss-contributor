"""The digest (DESIGN §14.13.4, AC 42): due once per period, silent P2 in 📈 OSS feed, off = not
generated, the §14.19 item 1 stats with "gained" deltas, and the merge-rate-by-size table."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from ossbot.common.clock import FakeClock
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import digest
from ossbot.gatekeeper.telegram.runtime import ui_of
from tests.conftest import insert_contribution
from tests.telegram.tgharness import TgRig, make_rig


class Reader:
    def __init__(self, followers: int, stars: int) -> None:
        self.f, self.s = followers, stars

    async def followers(self, login: str) -> int:
        return self.f

    async def stars(self, login: str) -> int:
        return self.s


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def _at_local(clock: FakeClock, ctx: AppContext, hh: int, mm: int, *, weekday: int | None = None) -> None:
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(ctx.config.profile.telegram.timezone)
    local = clock.now().astimezone(zone)
    target = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if weekday is not None:
        target += timedelta(days=(weekday - target.weekday()) % 7)
    clock.set(target.astimezone(UTC))


def _with_outcomes(ctx: AppContext) -> None:
    for result in ("merged", "merged", "closed"):
        cid = insert_contribution(ctx)
        ctx.conn.execute(
            "INSERT INTO outcomes (cid, upstream, kind, size_class, source, proposed_at, started_at,"
            " pr_opened_at, result, finished_at, updated_at) VALUES (?, 'o/r', 'bugfix', 'xs', 'discover',"
            " ?, ?, ?, ?, ?, ?)",
            (
                cid,
                *[datetime.now(UTC).isoformat().replace("+00:00", "Z")] * 3,
                result,
                *[datetime.now(UTC).isoformat().replace("+00:00", "Z")] * 2,
            ),
        )


async def test_daily_digest_runs_once_silent_p2_in_the_feed(rig: TgRig, clock: FakeClock) -> None:
    ctx = rig.ctx
    _at_local(clock, ctx, 21, 31)
    assert await digest.tick(ctx, Reader(10, 40)) >= 1
    assert await digest.tick(ctx, Reader(10, 40)) == 0  # one digest_runs row per period
    row = ctx.conn.execute("SELECT * FROM tg_outbox WHERE dedupe_key LIKE 'digest:daily:%'").fetchone()
    assert row["thread_key"] == "feed" and row["priority"] == 2
    payload = json.loads(row["payload_json"])
    assert payload.get("disable_notification") is True
    doc = json.dumps(payload, ensure_ascii=False)
    assert "👥 Followers: 10" in doc and "⭐ Stars: 40" in doc and "🔥 Streak:" in doc


async def test_gained_is_the_delta_to_the_previous_snapshot(rig: TgRig, clock: FakeClock) -> None:
    ctx = rig.ctx
    _at_local(clock, ctx, 21, 31)
    await digest.run(ctx, digest.now_period(ctx, "daily"), Reader(10, 40))
    clock.advance(timedelta(days=1))
    await digest.run(ctx, digest.now_period(ctx, "daily"), Reader(12, 39))
    rows = ctx.conn.execute("SELECT payload_json FROM tg_outbox WHERE dedupe_key LIKE 'digest:daily:%' ORDER BY id")
    last = json.dumps(json.loads(rows.fetchall()[-1][0]), ensure_ascii=False)
    assert "👥 Followers: 12 (+2)" in last and "⭐ Stars: 39 (-1)" in last


async def test_a_disabled_kind_is_not_generated(rig: TgRig, clock: FakeClock) -> None:
    ui_of(rig.ctx).notifier.set_toggle("digest_daily", False)
    _at_local(clock, rig.ctx, 21, 31)
    assert [p.kind for p in digest.due(rig.ctx)] in ([], ["weekly"])


async def test_weekly_digest_has_the_by_size_table(rig: TgRig, clock: FakeClock) -> None:
    ctx = rig.ctx
    _with_outcomes(ctx)
    _at_local(clock, ctx, 18, 1, weekday=6)
    await digest.tick(ctx, None)
    row = ctx.conn.execute("SELECT payload_json FROM tg_outbox WHERE dedupe_key LIKE 'digest:weekly:%'").fetchone()
    doc = json.dumps(json.loads(row["payload_json"]), ensure_ascii=False)
    assert "By size" in doc and "Auto size ≤" in doc and "tune with /size" in doc
    assert '"type": "table"' in doc or "class" in doc
