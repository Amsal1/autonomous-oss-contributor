"""Notification priorities (DESIGN §14.13.1, U7): P0 always rings and is never held, P1 rings, P2 is a
silent FYI in 📈 OSS feed; the local-operator-action FYI is a P2 that is never held."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from ossbot.common.clock import to_iso
from ossbot.gatekeeper.anomaly import Signal
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.notify import THREAD_OF, Notifier, classify


@pytest.fixture
def notifier(ctx: AppContext) -> Notifier:
    n = Notifier(ctx)
    ctx.outbox.hold_policy = n.decide
    return n


def _rows(ctx: AppContext) -> list[dict[str, object]]:
    rows = ctx.conn.execute("SELECT * FROM tg_outbox ORDER BY id").fetchall()
    return [{**dict(r), "payload": json.loads(r["payload_json"])} for r in rows]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("🛑 Killed (soft) by tg:1: x", (0, False)),
        ("🚨 kill flag missing; use /resume", (0, False)),
        ("🚨 P1 probe pending", (1, False)),
        ("⚠️ Local operator action: RESUME by uid 1000", (2, False)),
        ("🌐 Egress mode allowlist → research", (2, True)),
        ("⏸ Paused by anomaly: secret_in_text: x", (0, False)),
        ("⏸ Paused by uid:1001/sandbox: secret in output", (0, False)),
        ("⏸ Paused by uid:1002/worker: model_collision:reviewer", (0, False)),
        ("⏸ Paused by uid:0/operator: maintenance", (2, True)),
        ("⏸ Paused by tg:1: telegram /pause", (2, True)),
    ],
)
def test_classify_by_the_fixed_leading_icon(text: str, expected: tuple[int, bool]) -> None:
    assert classify(text) == expected


def test_topics_per_priority() -> None:
    assert THREAD_OF == {0: "control", 1: "control", 2: "feed", 3: "feed"}


def test_p0_rings_in_control_even_in_quiet_hours(ctx: AppContext, notifier: Notifier) -> None:
    notifier.prefs.set("quiet_until", to_iso(ctx.clock.now() + timedelta(hours=3)))
    ctx.notify("🛑 Killed (soft) by tg:1: test")
    [row] = _rows(ctx)
    assert row["priority"] == 0 and row["thread_key"] == "control" and row["status"] == "pending"
    assert row["payload"]["disable_notification"] is False  # type: ignore[index]


def test_an_anomaly_pause_rings_in_control_during_quiet_hours(ctx: AppContext, notifier: Notifier) -> None:
    """F009-R3-1: the 2nd secret hit in 24 h pauses; that pause is P0, never held (U7)."""
    notifier.prefs.set("quiet_until", to_iso(ctx.clock.now() + timedelta(hours=3)))
    ctx.anomaly.report(Signal.SECRET_IN_TEXT, "first")
    assert ctx.anomaly.report(Signal.SECRET_IN_TEXT, "second") == "pause"
    paused = [r for r in _rows(ctx) if str(r["payload"]["text"]).startswith("⏸ Paused by anomaly")]  # type: ignore[index]
    assert len(paused) == 1
    [row] = paused
    assert row["priority"] == 0 and row["thread_key"] == "control" and row["status"] == "pending"
    assert row["payload"]["disable_notification"] is False  # type: ignore[index]


def test_p2_fyi_is_silent_in_the_feed(ctx: AppContext, notifier: Notifier) -> None:
    ctx.notify("🌐 Egress mode allowlist → research by tg:1")
    [row] = _rows(ctx)
    assert row["priority"] == 2 and row["thread_key"] == "feed" and row["status"] == "pending"
    assert row["payload"]["disable_notification"] is True  # type: ignore[index]


def test_the_local_operator_action_fyi_is_never_held(ctx: AppContext, notifier: Notifier) -> None:
    notifier.prefs.set("quiet_until", to_iso(ctx.clock.now() + timedelta(hours=3)))
    ctx.notify("⚠️ Local operator action: RESUME by uid 1000 (pid 7) at 12:00")
    [row] = _rows(ctx)
    assert row["status"] == "pending" and row["payload"]["disable_notification"] is True  # type: ignore[index]


def test_p0_is_sent_before_older_feed_rows(ctx: AppContext, notifier: Notifier) -> None:
    ctx.notify("🌐 first FYI")
    ctx.notify("🛑 Killed (soft) by tg:1: test")
    assert [r.priority for r in ctx.outbox.due()] == [0, 2]


def test_a_muted_category_is_silent_but_still_delivered(ctx: AppContext, notifier: Notifier) -> None:
    """Toggles mute or batch, they never drop (§14.13.2): a P1 card category off is only silent."""
    notifier.set_toggle("approvals", False)
    ctx.outbox.enqueue("sendMessage", {"chat_id": 1, "text": "card"}, priority=1, category="approvals",
                       bind_tokens=["tok"])  # fmt: skip
    [row] = _rows(ctx)
    assert row["status"] == "pending" and row["payload"]["disable_notification"] is True  # type: ignore[index]
