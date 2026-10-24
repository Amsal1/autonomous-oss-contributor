"""Shared steps of the toolchain sandbox tests (build-env design §15 "Toolchains", AC 12): the task volume an
implement stage has after its one re-fetch (tests/sandbox/test_jvm_offline.py ``prepare``), a strict validate job,
the offline agent-exec of the coder pair (tests/sandbox/test_profiles.py ``execd_runs``), and the squid lines of
one job network (L-12: no TCP_DENIED and no request to mise-versions.jdx.dev or api.github.com)."""

from __future__ import annotations

import ipaddress
import time
from pathlib import Path
from typing import Any

from ossbot.common.egress_log import Entry, parse_line
from tests.sandbox.conftest import Sandbox
from tests.sandbox.test_jvm_offline import log, prepare, validate
from tests.sandbox.test_profiles import execd_runs

ACCESS_LOG = Path("/srv/ossbot/logs/proxy/access.log")
NEVER = ("mise-versions.jdx.dev", "api.github.com")

__all__ = ["NEVER", "assert_l12", "execd_runs", "job_lines", "log", "prepare", "tool", "tools", "validate"]


def tools(info: dict[str, Any], name: str) -> list[dict[str, Any]]:
    """The resolver's entries for ``name`` in a fetch.json (``toolchains``, build-env design §6.1)."""
    return [e for e in info.get("toolchains", []) if e.get("tool") == name]


def tool(info: dict[str, Any], name: str) -> dict[str, Any]:
    found = tools(info, name)
    assert len(found) == 1, (name, info.get("toolchains"))
    return found[0]


def job_lines(sandbox: Sandbox, cid: str, network: str, since: float) -> list[Entry]:
    """Every squid line from the cid's ``network`` /28 (``fetch-strict``, ``validate-strict``) since ``since``."""
    time.sleep(2)  # squid writes a tunnel's line when it closes
    subnet = ipaddress.IPv4Network(sandbox.svc.networks.subnets.allocate(network, cid))
    out = []
    for raw in ACCESS_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        e = parse_line(raw)
        if e is not None and e.ts >= since and ipaddress.IPv4Address(e.client) in subnet:
            out.append(e)
    return out


def assert_l12(lines: list[Entry]) -> None:
    """L-12: a strict install needs no host outside the base list and asks neither the mise versions host nor
    the GitHub API."""
    assert lines, "no squid lines for the job network"
    denied = [(e.result, e.host) for e in lines if e.result == "TCP_DENIED"]
    assert denied == [], denied
    assert not [e.host for e in lines if e.host in NEVER], [e.host for e in lines]
