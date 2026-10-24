"""ossbot-floorlog (build-env design §4.4, review 2 M1): kernel LOG lines -> floor-log lines, split by source,
heartbeats, rotation."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ossbot.watchdog import floorlog

pytestmark = pytest.mark.unit
TS_US = 1_792_600_000_123_456


def _journal(message: str, ts_us: int = TS_US) -> bytes:
    return json.dumps({"MESSAGE": message, "__REALTIME_TIMESTAMP": str(ts_us), "_TRANSPORT": "kernel"}).encode()


def _kernel(prefix: str, src: str, dst: str, proto: str = "TCP", dpt: str | None = "443") -> str:
    tail = f" SPT=40000 DPT={dpt} WINDOW=64240 RES=0x00 SYN URGP=0" if dpt is not None else " TYPE=8 CODE=0 ID=1"
    return (f"{prefix}IN=ossbot-egress OUT=eth0 MAC=02:42:0a:d4:00:02 SRC={src} DST={dst} LEN=60 TOS=0x00 "
            f"PREC=0x00 TTL=63 ID=4242 DF PROTO={proto}{tail}")  # fmt: skip


def test_journalctl_argv() -> None:
    assert floorlog.JOURNALCTL == ["/usr/bin/journalctl", "-k", "-f", "-n", "0", "-o", "json"]


@pytest.mark.parametrize(
    ("prefix", "src", "dst", "kind"),
    [
        ("OSSBOT-FLOOR-", "10.213.0.18", "10.213.0.17", "in"),
        ("OSSBOT-PFLOOR-", "10.212.0.2", "172.21.192.1", "px"),
        ("OSSBOT-TFLOOR-", "10.212.0.245", "10.0.0.1", "tx"),
        ("OSSBOT-PFLOOR-", "10.212.0.245", "10.0.0.1", "tx"),  # a PFLOOR line from another source: never px
    ],
)  # fmt: skip
def test_kinds_split_by_source(prefix: str, src: str, dst: str, kind: str) -> None:
    stats = floorlog.Stats()
    line = floorlog.parse_entry(_journal(_kernel(prefix, src, dst)), stats)
    assert line == f"1792600000.123 {kind} {src} {dst} TCP 443\n"
    if prefix == "OSSBOT-PFLOOR-" and kind == "tx":
        assert stats.counts["pfloor_src_mismatch"] == 1


@pytest.mark.parametrize(
    "message",
    [
        _kernel("OSSBOT-FLOOR-", "10.213.0.18", "10.213.0.17", proto="ICMP", dpt=None),  # no DPT: dropped
        _kernel("OSSBOT-FLOOR-", "fe80::1", "10.213.0.17"),
        _kernel("OSSBOT-FLOOR-", "10.213.0.18", "10.213.0.17", dpt="0"),
        _kernel("OSSBOT-FLOOR-", "10.213.0.18", "10.213.0.17", dpt="70000"),
        _kernel("OSSBOT-FLOOR-", "10.213.0.18", "10.213.0.17", proto="SCTP"),
        _kernel("OSSBOT-FLOOR-", "10.213.0.18", "10.213.0.17") + " SRC=10.9.9.9",  # ambiguous
    ],
)
def test_malformed_kernel_lines_are_counted_and_dropped(message: str) -> None:
    stats = floorlog.Stats()
    assert floorlog.parse_entry(_journal(message), stats) is None
    assert stats.counts["dropped"] == 1


def test_other_kernel_messages_and_bad_json_are_ignored() -> None:
    stats = floorlog.Stats()
    assert floorlog.parse_entry(_journal("audit: type=1400 apparmor=DENIED"), stats) is None
    assert floorlog.parse_entry(_journal("xOSSBOT-FLOOR-IN=eth0 SRC=1.1.1.1"), stats) is None  # anchored
    assert floorlog.parse_entry(b"{not json", stats) is None and stats.counts["unparseable"] == 1
    assert floorlog.parse_entry(json.dumps({"MESSAGE": [1, 2]}).encode(), stats) is None
    assert floorlog.parse_entry(b"x" * (floorlog.MAX_LINE + 1), stats) is None and stats.counts["oversize"] == 1


def test_heartbeat_matches_the_meter_regex() -> None:
    assert floorlog.heartbeat_line(1792600000.5) == "1792600000.500 hb - - HB 0\n"


def test_pump_writes_lines_heartbeats_and_rotates(tmp_path: Path) -> None:
    out = floorlog.FloorLog(tmp_path, group=None, rotate_bytes=60)
    r, w = os.pipe()
    lines = [_journal(_kernel("OSSBOT-PFLOOR-", "10.212.0.2", "10.0.0.1")),
             _journal(_kernel("OSSBOT-TFLOOR-", "10.212.0.245", "169.254.169.254", dpt="80")),
             _journal("unrelated")]  # fmt: skip
    os.write(w, b"\n".join(lines) + b"\n")
    os.close(w)
    clock = iter([0.0, 0.0, 1.0, 1.0, 40.0, 40.0, 40.0, 40.0, 41.0, 41.0])
    floorlog.pump(r, out, floorlog.Stats(), clock=lambda: next(clock, 99.0), heartbeat_s=30.0)
    os.close(r)
    out.close()
    text = "".join(p.read_text() for p in sorted(tmp_path.iterdir()))
    assert "0.000 hb - - HB 0\n" in text
    assert " px 10.212.0.2 10.0.0.1 TCP 443\n" in text and " tx 10.212.0.245 169.254.169.254 TCP 80\n" in text
    assert sorted(p.name for p in tmp_path.iterdir())[0] == "floor.log"
    assert any(p.name.startswith("floor.log.") for p in tmp_path.iterdir())  # rotated past 60 bytes
    assert oct((tmp_path / "floor.log").stat().st_mode & 0o777) == "0o640"
