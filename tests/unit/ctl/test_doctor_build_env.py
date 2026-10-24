"""Doctor's build-env floor checks (design §4.1, §4.7, N5, AC 23 doctor half): ``resolver_returns_floor_addresses``
(a fake-IP VPN DNS answer), ``public_floor_unknown``, the floor-log reader unit and ``host_floor_outside_static``."""

from __future__ import annotations

from typing import Any

import pytest

from ossbot.common.public_floor import FloorStatus
from ossbot.ctl import doctor


@pytest.fixture
def root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_is_root", lambda: True)


def _resolver(answers: dict[str, list[str]]) -> Any:
    return lambda name: answers[name]


@pytest.mark.parametrize(("name", "addr"), [("example.com", "198.18.0.5"), ("github.com", "10.1.2.3"),
                                            ("github.com", "fd00::5"), ("example.com", "127.0.0.1")])  # fmt: skip
def test_a_floor_answer_warns_with_the_vpn_hint(root: None, name: str, addr: str) -> None:
    answers = {"example.com": ["93.184.215.14"], "github.com": ["140.82.112.3"]}
    answers[name] = [addr, *answers[name]]
    result = doctor.check_resolver_floor(_resolver(answers))
    assert result.status == doctor.WARN and result.name == "resolver_returns_floor_addresses"
    assert result.detail.startswith("resolver_returns_floor_addresses:") and f"{name} -> {addr}" in result.detail
    assert "fake-IP" in result.hint and "198.18.0.0/15" in result.hint


def test_public_answers_pass(root: None) -> None:
    result = doctor.check_resolver_floor(_resolver({"example.com": ["93.184.215.14", "2606:2800:21f:cb07::1"],
                                                    "github.com": ["140.82.112.3"]}))  # fmt: skip
    assert result.status == doctor.PASS and "outside the floors" in result.detail


def test_a_failed_lookup_warns(root: None) -> None:
    def broken(name: str) -> list[str]:
        raise OSError("Temporary failure in name resolution")

    result = doctor.check_resolver_floor(broken)
    assert result.status == doctor.WARN and "lookup of example.com failed" in result.detail


def test_the_resolver_check_needs_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_is_root", lambda: False)
    calls: list[str] = []
    result = doctor.check_resolver_floor(lambda name: calls.append(name) or [])  # type: ignore[func-returns-value]
    assert result.status == doctor.SKIP and calls == []


def test_floor_answers_ignores_garbage() -> None:
    assert doctor.floor_answers({"x": ["not-an-ip", "8.8.8.8", "fe80::1%eth0"]}) == ["x -> fe80::1"]


def test_public_floor_unknown_warns() -> None:
    unknown = FloorStatus(False, 1_792_600_300.0, 0, 4, True)
    result = doctor.check_public_floor(lambda: unknown)
    assert result.status == doctor.WARN and result.detail.startswith("public_floor_unknown since ")
    assert "repo code's web is paused" in result.detail and "ossbot-pubip" in result.hint
    never = doctor.check_public_floor(lambda: FloorStatus(False, None, 0, 0, False))
    assert never.status == doctor.WARN and never.detail.startswith("public_floor_unknown:")


def test_public_floor_known_passes_with_a_count_only() -> None:
    result = doctor.check_public_floor(lambda: FloorStatus(True, None, 2, 9, True))
    assert result.status == doctor.PASS and result.detail == "known, 2 entries"


def test_host_floor_outside_static_warns() -> None:
    result = doctor.check_host_floor(lambda: ["192.0.2.10/32"])
    assert result.status == doctor.WARN and "host_floor_outside_static: 192.0.2.10/32" in result.detail
    assert doctor.check_host_floor(lambda: []).status == doctor.PASS


@pytest.mark.parametrize(("out", "status"), [("active\n", doctor.PASS), ("inactive\n", doctor.FAIL)])
def test_floorlog_unit_state(monkeypatch: pytest.MonkeyPatch, out: str, status: str) -> None:
    import subprocess

    def run(argv: list[str], timeout: float = 20.0, user: str | None = None) -> subprocess.CompletedProcess[str]:
        assert argv == ["/usr/bin/systemctl", "is-active", "ossbot-floorlog.service"]
        return subprocess.CompletedProcess(argv, 0 if status == doctor.PASS else 3, out, "")

    monkeypatch.setattr(doctor, "_run", run)
    result = doctor.check_floorlog(lambda: "ok")
    assert result.status == status and "floor detector ok" in result.detail
    if status == doctor.PASS:  # the unit runs, but sandboxd's meter misses its heartbeat
        assert doctor.check_floorlog(lambda: "down").status == doctor.WARN


def test_the_driver_runs_the_new_checks() -> None:
    import inspect

    src = inspect.getsource(doctor.run)
    for name in ("check_public_floor", "check_floorlog", "check_host_floor", "check_resolver_floor"):
        assert name in src
