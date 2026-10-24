"""Watchdog tick vs the CR-001 lifecycle (Phase 1 review P1-8) and the firewall drift reasons (P1-6)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ossbot.common.control_flags import read_flags, write_flag
from ossbot.watchdog import tick


@pytest.fixture
def calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    control = tmp_path / "control"
    control.mkdir()
    monkeypatch.setattr(tick, "CONTROL", control)
    monkeypatch.setattr(tick, "STATE", tmp_path / "watchdog.json")
    monkeypatch.setattr(tick.perms, "check_all", lambda: [])
    seen: list[str] = []
    monkeypatch.setattr(tick, "check_firewall", lambda t: seen.append("firewall"))
    monkeypatch.setattr(tick, "check_health", lambda t: seen.append("health"))
    monkeypatch.setattr(tick, "check_disk", lambda t: seen.append("disk"))

    def failed(argv: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
        if argv[1] == "is-failed":
            return subprocess.CompletedProcess(argv, 0, "", "")  # every unit "failed"
        seen.append(" ".join(argv[1:]))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(tick, "_run", failed)
    return seen


def _lifecycle(scope: str) -> dict[str, object]:
    return {"state": "down", "scope": scope, "reason": "r", "actor": "ossctl", "ts": "t", "timeout_s": 120}


def _started(seen: list[str]) -> set[str]:
    return {c.split()[-1] for c in seen if c.startswith("start ")}


def test_running_checks_health_and_restarts_failed_units(calls: list[str]) -> None:
    tick.run_tick()
    assert "health" in calls and "disk" in calls
    assert _started(calls) == set(tick.RESTARTABLE)


def test_distro_down_skips_health_and_restarts(calls: list[str]) -> None:
    write_flag(tick.CONTROL / "lifecycle", _lifecycle("distro"))
    tick.run_tick()
    assert "health" not in calls and "disk" in calls
    assert _started(calls) == set()


def test_workers_down_keeps_health_and_restarts_only_the_core(calls: list[str]) -> None:
    """The gatekeeper keeps running in the workers scope (and the flag survives reboots), so a
    dead gatekeeper must still be paused/restarted; the stopped workers stay stopped."""
    write_flag(tick.CONTROL / "lifecycle", _lifecycle("workers"))
    tick.run_tick()
    assert "health" in calls and "disk" in calls
    started = _started(calls)
    assert "ossbot-gatekeeper.service" in started and "ossbot-proxy.service" in started
    assert not started & set(tick.WORKER_UNITS)


@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        ("ossbot-firewall --check: iptables OSSBOT-OUT: rules differ", "firewall_drift"),
        ("ossbot-firewall --check: firewall_drift:resolver_changed", "firewall_drift:resolver_changed"),
        ("ossbot-firewall --check: firewall_drift:resolved_ipc systemd-resolved is not masked",
         "firewall_drift:resolved_ipc"),
        ("ossbot-firewall --check: firewall_drift:io_uring kernel.io_uring_disabled is 0, expected 2",
         "firewall_drift:io_uring"),
        ("ossbot-firewall --check: firewall_drift:squid_mode /etc/ossbot/proxy/squid.conf differs from the "
         "render for egress mode allowlist", "firewall_drift:squid_mode"),
        ("ossbot-firewall --check: firewall_drift:host_floor_changed the live host addresses moved",
         "firewall_drift:host_floor_changed"),
        ("ossbot-firewall --check: firewall_drift:public_floor OSSBOT-PUBFLOOR differs from the public floor file",
         "firewall_drift:public_floor"),
        ("ossbot-render: floor_lock_untrusted: /etc/ossbot/proxy/floor/.lock: mode 0644, expected 0600",
         "firewall_drift:floor_lock_untrusted"),
    ],
)  # fmt: skip
def test_firewall_drift_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: str, reason: str) -> None:
    monkeypatch.setattr(tick, "CONTROL", tmp_path)
    monkeypatch.setattr(tick.time, "sleep", lambda s: None)
    monkeypatch.setattr(tick, "_run", lambda argv, timeout=15.0: subprocess.CompletedProcess(argv, 1, "", stderr))
    t = tick.Tick({})
    tick.check_firewall(t)
    killed = read_flags(tmp_path).killed
    assert killed is not None and killed.reason == reason and killed.level == "soft"


def test_floor_lock_busy_skips_twice_then_counts_as_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """C-M1 / design §4.7: a busy shared floor lock skips the tick's firewall verdict (WARNING
    floor_lock_busy); the third consecutive skip is firewall_drift:floor_lock; a passing check resets it."""
    monkeypatch.setattr(tick, "CONTROL", tmp_path)
    monkeypatch.setattr(tick.time, "sleep", lambda s: None)
    rc = {"value": 2}
    busy = "ossbot-render: floor_lock_busy: /etc/ossbot/proxy/floor/.lock held by another process for more than 10 s"
    monkeypatch.setattr(tick, "_run", lambda argv, timeout=15.0: subprocess.CompletedProcess(argv, rc["value"], "",
                                                                                              busy))  # fmt: skip
    t = tick.Tick({})
    for expected in (1, 2):
        tick.check_firewall(t)
        assert t.state["floor_lock_busy"] == expected and read_flags(tmp_path).killed is None
    rc["value"] = 0
    tick.check_firewall(t)
    assert t.state["floor_lock_busy"] == 0
    rc["value"] = 2
    for _ in range(3):
        tick.check_firewall(t)
    killed = read_flags(tmp_path).killed
    assert killed is not None and killed.reason == "firewall_drift:floor_lock"
    assert [e["event"] for e in t.events].count("floor_lock_busy") == 5
