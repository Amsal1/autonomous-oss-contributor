"""ossbot-floorlog (build-env design §4.4, ADR-0025): the kernel floor drops, as lines sandboxd's meter reads.

A small root reader, no decisions: it runs ``journalctl -k -f -n 0 -o json`` (argv list) and keeps the kernel
messages of the rendered LOG rules (``ctl/render.py firewall_plan``): ``OSSBOT-FLOOR-`` (a sandbox packet at a
host address, OSSBOT-IN), ``OSSBOT-PFLOOR-`` (the live proxy's connection to a floor, a distro-local address or
the public floor: OSSBOT-FWD, OSSBOT-PUBFLOOR, OSSBOT-IN) and ``OSSBOT-TFLOOR-`` (another member of
ossbot-egress, i.e. a root test proxy). For each it appends ``"<ts.ms> <kind> <src> <dst> <proto> <dport>"`` to
``/srv/ossbot/logs/floor/floor.log`` (dir ``root:ossbox 0750``, file ``root:ossbox 0640``):

- ``in`` for ``OSSBOT-FLOOR-``;
- ``px`` for ``OSSBOT-PFLOOR-`` only when ``SRC=10.212.0.2`` (the rendered rules can't produce another source;
  one that does is written as ``tx`` and counted as ``pfloor_src_mismatch``);
- ``tx`` for ``OSSBOT-TFLOOR-`` (never holds, never notifies).

``SRC= DST= PROTO= DPT=`` are taken with anchored patterns: IPv4 only, ``PROTO`` TCP or UDP, ``DPT`` 1-65535;
anything else is counted and dropped. Every 30 s it appends the heartbeat ``"<ts.ms> hb - - HB 0"`` (the meter
reports ``floor_detector: down`` when it is older than 120 s) and logs the counters to the journal. At 64 MiB
the file is renamed ``floor.log.<UTC stamp>`` (the meter's reader is rename-safe; ``ossctl gc`` removes old
ones). When ``journalctl`` exits, so does this (non-zero) and systemd restarts it.

Imports only ``ossbot.common`` (import-linter contract).
"""

from __future__ import annotations

import grp
import ipaddress
import json
import logging
import os
import re
import select
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("ossbot.floorlog")

JOURNALCTL = ["/usr/bin/journalctl", "-k", "-f", "-n", "0", "-o", "json"]
LOG_DIR = Path("/srv/ossbot/logs/floor")
LOG_NAME = "floor.log"
LOG_GROUP = "ossbox"
PROXY_IP = "10.212.0.2"  # ctl/render.py PROXY_IP (watchdog may not import ctl)
HEARTBEAT_S = 30.0
ROTATE_BYTES = 64 * 1024 * 1024
MAX_LINE = 64 * 1024  # a journal JSON line longer than this is dropped (counted)
_PREFIX_RE = re.compile(r"^OSSBOT-(FLOOR|PFLOOR|TFLOOR)-IN=")
_FIELD_RE = {k: re.compile(rf"(?:^| ){k}=(\S+)(?= |$)") for k in ("SRC", "DST", "PROTO", "DPT")}
_KIND = {"FLOOR": "in", "PFLOOR": "px", "TFLOOR": "tx"}


@dataclass
class Stats:
    counts: Counter[str] = field(default_factory=Counter)

    def add(self, key: str) -> None:
        self.counts[key] += 1


def _ipv4(value: str) -> str | None:
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        return None


def parse_message(message: str, stats: Stats) -> tuple[str, str, str, str, int] | None:
    """(kind, src, dst, proto, dport) of one kernel message, or None (not ours, or counted as dropped)."""
    m = _PREFIX_RE.match(message)
    if m is None:
        return None
    values = {}
    for key, rx in _FIELD_RE.items():
        found = rx.findall(message)
        if len(found) != 1:  # missing (ICMP has no DPT) or ambiguous: never guess
            stats.add("dropped")
            return None
        values[key] = found[0]
    src, dst = _ipv4(values["SRC"]), _ipv4(values["DST"])
    proto, dport = values["PROTO"], values["DPT"]
    if src is None or dst is None or proto not in ("TCP", "UDP") or not dport.isdigit() or not 1 <= int(dport) <= 65535:
        stats.add("dropped")
        return None
    kind = _KIND[m.group(1)]
    if kind == "px" and src != PROXY_IP:
        stats.add("pfloor_src_mismatch")
        kind = "tx"
    stats.add(kind)
    return kind, src, dst, proto, int(dport)


def format_line(ts: float, kind: str, src: str, dst: str, proto: str, dport: int) -> str:
    return f"{ts:.3f} {kind} {src} {dst} {proto} {dport}\n"


def heartbeat_line(ts: float) -> str:
    return f"{ts:.3f} hb - - HB 0\n"


def parse_entry(raw: bytes, stats: Stats) -> str | None:
    """One ``journalctl -o json`` line -> the floor-log line, or None."""
    if len(raw) > MAX_LINE:
        stats.add("oversize")
        return None
    try:
        entry: Any = json.loads(raw)
    except ValueError:
        stats.add("unparseable")
        return None
    message = entry.get("MESSAGE") if isinstance(entry, dict) else None
    if not isinstance(message, str):  # journald sends non-UTF-8 messages as byte arrays: not ours
        return None
    parsed = parse_message(message, stats)
    if parsed is None:
        return None
    try:
        ts = int(entry["__REALTIME_TIMESTAMP"]) / 1_000_000
    except (KeyError, TypeError, ValueError):
        ts = time.time()
    return format_line(ts, *parsed)


class FloorLog:
    """The output file: append, rotate at ``ROTATE_BYTES``, root:ossbox 0640."""

    def __init__(self, directory: Path = LOG_DIR, group: str | None = LOG_GROUP,
                 rotate_bytes: int = ROTATE_BYTES) -> None:  # fmt: skip
        self.directory = directory
        self.path = directory / LOG_NAME
        self.gid = -1
        if group is not None:
            try:
                self.gid = grp.getgrnam(group).gr_gid
            except KeyError:
                self.gid = -1
        self.rotate_bytes = rotate_bytes
        self.fd = self._open()

    def _open(self) -> int:
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640)
        if self.gid >= 0:
            os.fchown(fd, 0 if os.geteuid() == 0 else -1, self.gid)
        os.fchmod(fd, 0o640)
        return fd

    def write(self, line: str) -> None:
        os.write(self.fd, line.encode("ascii"))
        if os.fstat(self.fd).st_size >= self.rotate_bytes:
            self.rotate()

    def rotate(self) -> None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        os.close(self.fd)
        os.rename(self.path, self.directory / f"{LOG_NAME}.{stamp}")
        self.fd = self._open()

    def close(self) -> None:
        os.close(self.fd)


def pump(fd: int, out: FloorLog, stats: Stats, clock: Callable[[], float] = time.time,
         heartbeat_s: float = HEARTBEAT_S, should_stop: Callable[[], bool] = lambda: False) -> None:  # fmt: skip
    """Read ``journalctl -o json`` lines from ``fd`` until EOF, writing floor lines and heartbeats."""
    buf = b""
    out.write(heartbeat_line(clock()))
    next_hb = clock() + heartbeat_s
    while not should_stop():
        ready, _, _ = select.select([fd], [], [], max(0.0, next_hb - clock()))
        if ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                return  # journalctl exited
            buf += chunk
            *lines, buf = buf.split(b"\n")
            if len(buf) > MAX_LINE:
                stats.add("oversize")
                buf = b""
            for raw in lines:
                line = parse_entry(raw, stats) if raw.strip() else None
                if line is not None:
                    out.write(line)
        if clock() >= next_hb:
            out.write(heartbeat_line(clock()))
            next_hb = clock() + heartbeat_s
            if stats.counts:
                log.info("floor log: %s", dict(sorted(stats.counts.items())), extra={"event": "floorlog_heartbeat"})
                stats.counts.clear()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    out = FloorLog()
    stats = Stats()
    proc = subprocess.Popen(JOURNALCTL, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
    if proc.stdout is None:  # pragma: no cover - Popen with PIPE always sets it
        return 1
    try:
        pump(proc.stdout.fileno(), out, stats)
    finally:
        proc.kill()
        proc.wait(timeout=10)
        out.close()
    log.error("journalctl exited (%s); restarting through systemd", proc.returncode,
              extra={"event": "floorlog_journal_exit"})  # fmt: skip
    return 1


if __name__ == "__main__":
    sys.exit(main())
