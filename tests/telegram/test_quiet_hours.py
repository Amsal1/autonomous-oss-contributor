"""Quiet hours and /quiet (DESIGN §14.13.2): P1 goes silent, P2 is held until the end and then merged per
topic; the first loud message afterwards is one "🌅 While you were away" summary."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from ossbot.common.clock import to_iso
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.notify import Notifier, quiet_hours_end


@pytest.fixture
def notifier(ctx: AppContext) -> Notifier:
    n = Notifier(ctx)
    ctx.outbox.hold_policy = n.decide
    return n


def _at(h: int, m: int = 0, day: int = 17) -> datetime:
    return datetime(2026, 10, day, h, m, tzinfo=UTC)


@pytest.mark.parametrize(
    ("now", "end"),
    [
        (_at(23, 30), _at(7, 30, 18)),  # wraps midnight
        (_at(3, 0), _at(7, 30)),
        (_at(12, 0), None),
        (_at(7, 30), None),  # the end minute is outside
    ],
)
def test_quiet_hours_window_wraps_midnight(now: datetime, end: datetime | None) -> None:
    assert quiet_hours_end("23:00-07:30", "UTC", now) == end


def test_quiet_hours_follow_the_owner_timezone() -> None:
    # 23:30 in Jakarta (UTC+7) is 16:30 UTC; the window ends 07:30 Jakarta = 00:30 UTC next day
    assert quiet_hours_end("23:00-07:30", "Asia/Jakarta", _at(16, 30)) == _at(0, 30, 18)
    assert quiet_hours_end(None, "UTC", _at(23, 30)) is None


def test_quiet_holds_p2_silences_p1_and_summarises_after(ctx: AppContext, notifier: Notifier) -> None:
    until = ctx.clock.now() + timedelta(hours=1)
    notifier.prefs.set("quiet_until", to_iso(until))
    ctx.notify("📈 discovery found 3")
    ctx.notify("✅ CI passed on c_1")
    p1 = ctx.outbox.enqueue("sendMessage", {"chat_id": 1, "text": "maintainer replied"}, priority=1)
    held = ctx.conn.execute("SELECT next_attempt_at FROM tg_outbox WHERE status = 'held'").fetchall()
    assert [r[0] for r in held] == [to_iso(until)] * 2
    [row] = ctx.outbox.due()
    assert row.id == p1
    prepared = notifier.prepare(row)
    assert prepared.payload["disable_notification"] is True
    assert notifier.release() == 0  # still quiet: nothing is released

    ctx.clock.advance(hours=1, seconds=1)  # type: ignore[attr-defined]
    assert notifier.release() == 2
    rows = ctx.conn.execute("SELECT * FROM tg_outbox ORDER BY id").fetchall()
    merged = [r for r in rows if r["status"] == "merged"]
    assert len(merged) == 2 and len({r["merged_into"] for r in merged}) == 1
    texts = [json.loads(r["payload_json"]).get("text", "") for r in rows if r["status"] == "pending"]
    assert any(t.startswith("🌅 While you were away: 1 message") for t in texts)
    batch = db.one(ctx.conn, "SELECT payload_json FROM tg_outbox WHERE id = ?", (merged[0]["merged_into"],))
    body = json.loads(batch["payload_json"])
    assert "2 updates" in json.dumps(body, ensure_ascii=False)
    assert "discovery found 3" in json.dumps(body, ensure_ascii=False)


def test_a_single_held_row_is_released_unchanged(ctx: AppContext, notifier: Notifier) -> None:
    notifier.prefs.set("quiet_until", to_iso(ctx.clock.now() + timedelta(minutes=5)))
    rid = ctx.notify("📈 one FYI")
    del rid
    ctx.clock.advance(minutes=6)  # type: ignore[attr-defined]
    assert notifier.release() == 1
    [row] = ctx.outbox.due()
    assert row.payload["text"] == "📈 one FYI"
