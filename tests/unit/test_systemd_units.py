"""CR-001: every ossbot service unit gives a graceful stop at least the drain timeout + 30 s, and
systemd's reverse start order stops the workers before the gatekeeper and the proxy."""

from __future__ import annotations

import configparser
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from ossbot.common.config import LifecyclePolicy
from ossbot.common.control_flags import DEFAULT_DRAIN_TIMEOUT_S
from ossbot.ctl import cli

UNITS = Path(__file__).resolve().parents[2] / "systemd"
SERVICES = sorted(UNITS.glob("ossbot-*.service"))


def _unit(path: Path) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string(path.read_text(encoding="utf-8"))
    return parser


def _seconds(value: str) -> int:
    match = re.fullmatch(r"(\d+)(s|min)?", value.strip())
    assert match, value
    return int(match.group(1)) * (60 if match.group(2) == "min" else 1)


@pytest.mark.parametrize("path", SERVICES, ids=lambda p: p.name)
def test_timeout_stop_covers_the_drain(path: Path) -> None:
    service = _unit(path)["Service"]
    assert _seconds(service["TimeoutStopSec"]) >= LifecyclePolicy().drain_timeout_s + 30


def test_sandboxd_drain_fits_its_stop_timeout() -> None:
    text = (UNITS / "ossbot-sandboxd.service").read_text(encoding="utf-8")
    service = _unit(UNITS / "ossbot-sandboxd.service")["Service"]
    lines = [line.split("=", 1)[1] for line in text.splitlines() if line.startswith("Environment=")]
    env = dict(e.split("=", 1) for line in lines for e in line.split() if "=" in e)
    assert int(env["OSSBOT_DRAIN_TIMEOUT_S"]) + 30 <= _seconds(service["TimeoutStopSec"])


def test_policy_drain_timeout_cannot_exceed_the_static_unit_values() -> None:
    """P1-9: TimeoutStopSec, the sandboxd SIGTERM drain and the down.ps1 cut-off are static, so the
    policy may not ask for a longer drain than they cover."""
    assert LifecyclePolicy(drain_timeout_s=DEFAULT_DRAIN_TIMEOUT_S).drain_timeout_s == DEFAULT_DRAIN_TIMEOUT_S
    with pytest.raises(ValidationError):
        LifecyclePolicy(drain_timeout_s=DEFAULT_DRAIN_TIMEOUT_S + 1)
    text = (UNITS / "ossbot-sandboxd.service").read_text(encoding="utf-8")
    assert f"OSSBOT_DRAIN_TIMEOUT_S={DEFAULT_DRAIN_TIMEOUT_S}" in text
    down_ps1 = (UNITS.parent / "scripts" / "down.ps1").read_text(encoding="utf-8")
    assert f"else {{ {DEFAULT_DRAIN_TIMEOUT_S} }}" in down_ps1


NON_ROOT = [p for p in SERVICES if _unit(p)["Service"].get("User", "root") != "root"]


def test_non_root_units_are_known() -> None:
    """The restriction test below must not pass vacuously; Phase 2 runner/hermes units join it."""
    assert {"ossbot-gatekeeper.service", "ossbot-sandboxd.service"} <= {p.name for p in NON_ROOT}


@pytest.mark.parametrize("path", NON_ROOT, ids=lambda p: p.name)
def test_non_root_units_restrict_address_families(path: Path) -> None:
    """P1-13: AF_VSOCK reaches the Windows host (WSL init, the drvfs Plan 9 server) past every IP
    layer, so every non-root unit may open only unix and IP sockets."""
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.startswith("RestrictAddressFamilies=")]
    assert lines == ["RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6"]


@pytest.mark.parametrize("path", NON_ROOT, ids=lambda p: p.name)
def test_non_root_units_filter_io_uring(path: Path) -> None:
    """P1-14: IORING_OP_SOCKET creates sockets without socket(2), so the address-family filter above
    never sees them. Every non-root unit refuses the io_uring syscalls (EPERM, so a library that
    probes io_uring falls back instead of dying on SIGSYS), even if kernel.io_uring_disabled is lost."""
    service = _unit(path)["Service"]
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.startswith("SystemCallFilter=")]
    assert lines == ["SystemCallFilter=~io_uring_setup io_uring_enter io_uring_register"]
    assert service["SystemCallErrorNumber"] == "EPERM"
    # no 32-bit/x32 ABI either: socketcall(2) would be a second socket entry point past both filters
    assert service["SystemCallArchitectures"] == "native"


def test_workers_stop_before_gatekeeper_and_proxy() -> None:
    after = _unit(UNITS / "ossbot-sandboxd.service")["Unit"]["After"].split()
    assert "ossbot-gatekeeper.service" in after and "ossbot-proxy.service" in after  # stop = reverse order
    order = [*cli.WORKER_UNITS, cli.WATCHDOG_TIMER, *cli.CORE_UNITS]
    assert order.index("ossbot-sandboxd.service") < order.index("ossbot-gatekeeper.service")
    assert order[-2:] == ["ossbot-gatekeeper.service", "ossbot-proxy.service"]
