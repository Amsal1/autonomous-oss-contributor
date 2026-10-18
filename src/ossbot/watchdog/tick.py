"""Watchdog tick (DESIGN §13.2, §13.4), root, every 30 s (``ossbot-watchdog.timer``).

Per tick, in order:

1. permission invariants of §7.3 (``perms``): drift -> kill (soft) ``perm_drift``;
2. ``ossbot-firewall --check`` (re-checked up to 3 x 3 s to ride out a Docker restart): drift ->
   kill (soft) ``firewall_drift`` / ``firewall_drift:resolver_changed``;
3. while killed: a running ``ossbot.sandbox=1`` container or an active unit the kill stops ->
   re-enforce (``ossbot-kill --enforce``) + alert;
4. while running: gatekeeper ``/v1/health`` failing 4 consecutive ticks -> ``paused``
   (``gatekeeper_unhealthy``); ``/srv`` free < 20 GB -> ``paused`` (``disk_low``);
5. every 10th tick an audit anchor; every 120th tick (1 h) ``audit verify`` against the anchors
   -> kill on mismatch;
6. while running only: units systemd gave up on (``failed``) are reset and started once.

It never starts or restarts an ossbot unit while ``killed`` or ``paused`` exists. Everything it
does is logged to ``/srv/ossbot/logs/watchdog.log`` (JSON lines) and the journal.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ossbot.common.control_flags import read_flags, read_lifecycle, write_flag
from ossbot.common.uds import uds_client
from ossbot.gatekeeper.anchors import parse_anchors
from ossbot.gatekeeper.audit import verify
from ossbot.watchdog import anchors, perms

CONTROL = Path("/var/lib/ossbot/control")
STATE = Path("/var/lib/ossbot/watchdog.json")
LOG = Path("/srv/ossbot/logs/watchdog.log")
GATEKEEPER_SOCK = Path("/run/ossbot/gatekeeper.sock")
FIREWALL = "/opt/ossbot/bin/ossbot-firewall"
KILL = "/opt/ossbot/bin/ossbot-kill"
SYSTEMCTL = "/usr/bin/systemctl"
DOCKER = "/usr/bin/docker"
UNHEALTHY_TICKS = 4
ANCHOR_EVERY = 10
VERIFY_EVERY = 120
DISK_MIN_BYTES = 20 * 1024**3
SLEPT_GAP_S = 300  # > 10 missed ticks: the laptop slept / the VM was frozen (CR-001 item 7)
SOFT_KILL_UNITS = ("ossbot-runner.service", "ossbot-sandboxd.service", "ossbot-hermes.service")
FULL_KILL_UNITS = ("ossbot-gatekeeper.service", "ossbot-proxy.service")
RESTARTABLE = ("ossbot-dockerfilter.service", "ossbot-gatekeeper.service", "ossbot-proxy.service",
               "ossbot-sandboxd.service")  # fmt: skip


@dataclass
class Tick:
    state: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)

    def event(self, kind: str, **data: Any) -> None:
        doc = {"ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "event": kind, **data}
        self.events.append(doc)
        print(json.dumps(doc, sort_keys=True), file=sys.stderr)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(argv: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 124, "", str(exc))


def _load_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    tmp = STATE.with_name(f".{STATE.name}.tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE)


def kill(t: Tick, reason: str) -> None:
    """Soft kill (levels only escalate; an existing flag is left alone)."""
    if read_flags(CONTROL).killed is not None:
        return
    write_flag(CONTROL / "killed", {"level": "soft", "reason": reason[:200], "actor": "watchdog", "ts": _now()})
    t.event("kill", reason=reason)


def pause(t: Tick, reason: str) -> None:
    flags = read_flags(CONTROL)
    if flags.paused is not None or flags.killed is not None:
        return
    write_flag(CONTROL / "paused", {"reason": reason, "actor": "watchdog", "ts": _now()})
    t.event("pause", reason=reason)


def unit_active(unit: str) -> bool:
    return _run([SYSTEMCTL, "is-active", "--quiet", unit]).returncode == 0


def check_firewall(t: Tick) -> None:
    out = _run([FIREWALL, "--check"])
    for _ in range(3):  # a Docker (re)start briefly lacks DOCKER-USER until PartOf re-applies it
        if out.returncode == 0:
            break
        time.sleep(3)
        out = _run([FIREWALL, "--check"])
    if out.returncode != 0:
        reason = "firewall_drift:resolver_changed" if "resolver_changed" in out.stderr else "firewall_drift"
        t.event("firewall_drift", detail=out.stderr.strip()[-500:])
        kill(t, reason)


def reenforce_kill(t: Tick, level: str) -> None:
    units = SOFT_KILL_UNITS + (FULL_KILL_UNITS if level == "full" else ())
    running = [u for u in units if unit_active(u)]
    ps = _run([DOCKER, "ps", "-q", "--filter", "label=ossbot.sandbox=1"])
    containers = ps.stdout.split() if ps.returncode == 0 else []
    if running or containers:
        t.event("kill_reenforced", units=running, containers=len(containers))
        _run([KILL, "--enforce"], timeout=60)


def check_health(t: Tick) -> None:
    ok = False
    try:
        with uds_client(GATEKEEPER_SOCK, timeout=httpx.Timeout(5.0)) as client:
            ok = client.get("/v1/health").status_code == 200
    except (httpx.HTTPError, OSError):
        ok = False
    failures = 0 if ok else int(t.state.get("health_failures", 0)) + 1
    t.state["health_failures"] = failures
    if failures >= UNHEALTHY_TICKS:
        t.event("gatekeeper_unhealthy", ticks=failures)
        pause(t, "gatekeeper_unhealthy")


def check_disk(t: Tick) -> None:
    try:
        free = shutil.disk_usage("/srv").free
    except OSError:
        return
    if free < DISK_MIN_BYTES:
        t.event("disk_low", free_bytes=free)
        pause(t, "disk_low")


def audit_check(t: Tick) -> None:
    if not anchors.DB_PATH.exists():
        return
    found, errors = parse_anchors(anchors.ANCHOR_LOG)
    conn = sqlite3.connect(f"file:{anchors.DB_PATH}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        result = verify(conn, found)
    finally:
        conn.close()
    if errors or not result.ok:
        t.event("audit_anchor_mismatch", errors=[*errors, *result.errors][:20])
        kill(t, "audit_anchor_mismatch")


def restart_failed(t: Tick) -> None:
    for unit in RESTARTABLE:
        if _run([SYSTEMCTL, "is-failed", "--quiet", unit]).returncode == 0:
            _run([SYSTEMCTL, "reset-failed", unit])
            _run([SYSTEMCTL, "start", "--no-block", unit])
            t.event("unit_restarted", unit=unit)


def note_sleep(t: Tick, now: float) -> None:
    """CR-001 item 7: a wall-clock gap far beyond the 30 s tick means the laptop slept or the VM
    was frozen. Failures counted before the gap are not evidence of a sick gatekeeper (the poller
    and the gatekeeper reconnect after resume), so the counter restarts."""
    last = t.state.get("last_tick_wall")
    t.state["last_tick_wall"] = now
    if isinstance(last, int | float) and now - last > SLEPT_GAP_S:
        t.event("slept", gap_s=round(now - last))
        t.state["health_failures"] = 0


def run_tick() -> Tick:
    t = Tick(_load_state())
    count = int(t.state.get("tick", 0)) + 1
    t.state["tick"] = count
    note_sleep(t, time.time())
    drift = perms.check_all()
    if drift:
        t.event("perm_drift", problems=drift[:20])
        kill(t, f"perm_drift:{drift[0][:150]}")
    check_firewall(t)
    flags = read_flags(CONTROL)
    lifecycle = read_lifecycle(CONTROL)
    down = lifecycle is not None and lifecycle.state == "down"
    if flags.killed is not None:
        reenforce_kill(t, flags.killed.level)
        t.state["health_failures"] = 0
    elif down:  # CR-001: `ossctl down` stops the units on purpose; that is not "unhealthy"
        t.state["health_failures"] = 0
        check_disk(t)
    elif flags.paused is None:
        check_health(t)
        check_disk(t)
    if count % ANCHOR_EVERY == 0:
        try:
            line = anchors.append_anchor()
            if line:
                t.event("anchor", seq=line["seq"])
        except OSError as exc:
            t.event("anchor_failed", error=str(exc))
    if count % VERIFY_EVERY == 0:
        audit_check(t)
    if read_flags(CONTROL).running and not down:
        restart_failed(t)
    return t


def main() -> int:
    t = run_tick()
    _save_state(t.state)
    if t.events:
        try:
            with LOG.open("a", encoding="utf-8") as fh:
                for doc in t.events:
                    fh.write(json.dumps(doc, sort_keys=True) + "\n")
        except OSError as exc:
            print(f"watchdog log: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
