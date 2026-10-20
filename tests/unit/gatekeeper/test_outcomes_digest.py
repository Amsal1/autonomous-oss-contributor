"""Digest numbers from the track record (DESIGN §14.13.4, AC 42): merge rate by size class with ``l+``
merging, ``👤 yours``, rates only from 3 PRs, and the streak."""

from __future__ import annotations

from datetime import date

from ossbot.gatekeeper import outcomes
from ossbot.gatekeeper.context import AppContext
from tests.conftest import insert_contribution

T = "2026-10-01T00:00:00Z"


def _outcome(ctx: AppContext, cls: str, result: str | None, *, pr: bool = True, source: str = "discover") -> None:
    cid = insert_contribution(ctx, source=source)
    ctx.conn.execute(
        "INSERT INTO outcomes (cid, upstream, kind, size_class, source, proposed_at, started_at, pr_opened_at, result,"
        " updated_at) VALUES (?, 'o/r', 'bugfix', ?, ?, ?, ?, ?, ?, ?)",
        (cid, cls, source, T, T, T if pr else None, result, T),
    )  # fmt: skip


def test_merge_rate_rows_merge_big_classes_and_count_yours(ctx: AppContext) -> None:
    for result in ("merged", "merged", "closed", None):
        _outcome(ctx, "xs", result)
    _outcome(ctx, "l", "merged")
    _outcome(ctx, "xl", "closed")
    _outcome(ctx, "s", "dry_run")  # rehearsals never count
    _outcome(ctx, "m", "merged", source="user")
    rows = {r.label: r for r in outcomes.merge_rate_rows(ctx.conn, "2026-09-01T00:00:00Z")}
    assert set(rows) == {"xs", "l+", "👤 yours"}
    assert (rows["xs"].prs, rows["xs"].merged, rows["xs"].closed, rows["xs"].rate) == (4, 2, 1, "67%")
    assert (rows["l+"].prs, rows["l+"].merged, rows["l+"].rate) == (2, 1, "—")  # below 3 PRs: no rate
    assert rows["👤 yours"].proposed == 1


def test_a_big_class_with_three_prs_gets_its_own_row(ctx: AppContext) -> None:
    for result in ("merged", "merged", "closed"):
        _outcome(ctx, "l", result)
    _outcome(ctx, "xl", "merged")
    labels = [r.label for r in outcomes.merge_rate_rows(ctx.conn, "2026-09-01T00:00:00Z")]
    assert labels == ["l", "l+"]


def test_streak_ends_today_or_yesterday() -> None:
    today = date(2026, 10, 19)
    days = {date(2026, 10, 18), date(2026, 10, 17), date(2026, 10, 15)}
    assert outcomes.streak(days, today) == 2
    assert outcomes.streak(days | {today}, today) == 3
    assert outcomes.streak({date(2026, 10, 16)}, today) == 0
