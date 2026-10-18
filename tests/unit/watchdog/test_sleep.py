"""CR-001 item 7: a large wall-clock gap between ticks means the laptop slept; failures counted
before it are not evidence against the gatekeeper."""

from __future__ import annotations

from ossbot.watchdog import tick


def test_gap_resets_health_failures() -> None:
    t = tick.Tick({"health_failures": 3, "last_tick_wall": 1_000.0})
    tick.note_sleep(t, 1_000.0 + tick.SLEPT_GAP_S + 1)
    assert t.state["health_failures"] == 0
    assert [e["event"] for e in t.events] == ["slept"]


def test_normal_tick_keeps_counting() -> None:
    t = tick.Tick({"health_failures": 2, "last_tick_wall": 1_000.0})
    tick.note_sleep(t, 1_031.0)
    assert t.state["health_failures"] == 2 and t.events == []
    assert t.state["last_tick_wall"] == 1_031.0


def test_first_tick_has_no_baseline() -> None:
    t = tick.Tick({})
    tick.note_sleep(t, 5.0)
    assert t.events == [] and t.state["last_tick_wall"] == 5.0
