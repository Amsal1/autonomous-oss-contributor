"""CR-001: every ossbot service unit gives a graceful stop at least the drain timeout + 30 s, and
systemd's reverse start order stops the workers before the gatekeeper and the proxy."""

from __future__ import annotations

import configparser
import re
from pathlib import Path

import pytest

from ossbot.common.config import LifecyclePolicy
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


def test_workers_stop_before_gatekeeper_and_proxy() -> None:
    after = _unit(UNITS / "ossbot-sandboxd.service")["Unit"]["After"].split()
    assert "ossbot-gatekeeper.service" in after and "ossbot-proxy.service" in after  # stop = reverse order
    order = [*cli.WORKER_UNITS, cli.WATCHDOG_TIMER, *cli.CORE_UNITS]
    assert order.index("ossbot-sandboxd.service") < order.index("ossbot-gatekeeper.service")
    assert order[-2:] == ["ossbot-gatekeeper.service", "ossbot-proxy.service"]
