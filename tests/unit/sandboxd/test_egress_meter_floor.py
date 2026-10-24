"""The meter's guard policy and floor alarm (build-env design §2.3, §4.4, §4.5, §5.1, ADR-0025): a volume breach
is a note only for a web job of sandbox_full (at its start and now); floor marks and kernel lines hold in every
mode; loopback, quiet and public-literal lines never hold; ``in`` lines by /28 and window (finish + 3 s, late
lines within 30 s); ``px`` lines only from the live proxy, to every running job, a notice with none running;
``tx`` and other-source ``px`` lines never hold or notify; the heartbeat; the late-check ring buffer's
oldest-first eviction with the ``late_check_evicted`` counter and one INFO line per job (C-N5)."""

from __future__ import annotations

import ipaddress
import logging
from pathlib import Path
from typing import Any

import pytest

from ossbot.common.config import EgressAnomaly
from ossbot.common.egress_log import Floors
from ossbot.sandboxd.egress_meter import EgressMeter, floors_from_squid_conf

pytestmark = pytest.mark.unit
T0 = 1_792_600_000.0
NET = "10.213.0.16/28"
NET2 = "10.213.0.32/28"
LIMITS = EgressAnomaly(max_hosts=5, max_upload_mb=1, max_download_mb=64)
CID, CID2 = "c_01ABCDEFGH", "c_01ABCDEFGJ"
MIB = 1024 * 1024


def sq(ts: float, client: str, host: str, *, result: str = "TCP_TUNNEL/200", server: str = "1.2.3.4",
       mark: str = "-", up: int = 10, down: int = 10) -> str:  # fmt: skip
    return f"{ts:.3f} {client} 3128 {result} CONNECT {host}:443 {server} {up} {down} 50 {mark}\n"


def fl(ts: float, kind: str, src: str, dst: str, proto: str = "TCP", dport: int = 443) -> str:
    return f"{ts:.3f} {kind} {src} {dst} {proto} {dport}\n"


def hb(ts: float) -> str:
    return f"{ts:.3f} hb - - HB 0\n"


class Rig:
    def __init__(self, tmp_path: Path, mode: str = "sandbox_full", ring_max: int = 50_000) -> None:
        self.now = [T0 + 10]
        self.mode = [mode]
        self.squid = tmp_path / "proxy" / "access.log"
        self.floor = tmp_path / "floor" / "floor.log"
        for p in (self.squid, self.floor):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("", encoding="utf-8")
        self.events: dict[str, list[tuple[str, dict[str, Any]]]] = {"breach": [], "floor": [], "note": []}
        self.notices: list[str] = []
        self.meter = EgressMeter(
            self.squid, tmp_path / "runs", lambda: LIMITS, wall=lambda: self.now[0], sleep=lambda s: None,
            floor_log=self.floor, current_mode=lambda: self.mode[0], notify=self.notices.append,
            on_breach=lambda r, d: self.events["breach"].append((r, d)),
            on_floor=lambda r, d: self.events["floor"].append((r, d)),
            on_note=lambda r, d: self.events["note"].append((r, d)), ring_max=ring_max,
        )  # fmt: skip
        self.meter.prime()
        self.write_floor(hb(self.now[0]))

    def write_squid(self, *lines: str) -> None:
        with self.squid.open("a", encoding="utf-8") as fh:
            fh.writelines(lines)

    def write_floor(self, *lines: str) -> None:
        with self.floor.open("a", encoding="utf-8") as fh:
            fh.writelines(lines)

    def job(self, run_id: str = "r1", cid: str = CID, subnet: str = NET, mode: str = "sandbox_full",
            net: str = "web", stage: str = "validate", start: float = T0) -> None:  # fmt: skip
        self.meter.register(run_id, cid, stage, subnet, start, "mode", mode, net)

    def runs(self, kind: str) -> list[str]:
        return [r for r, _ in self.events[kind]]


def _over_volume(rig: Rig, client: str = "10.213.0.18") -> None:
    rig.write_squid(*[sq(T0 + 1 + i, client, f"h{i}.example") for i in range(7)])  # 7 hosts > 5


# ----------------------------------------------------------------------------------------- volume policy
@pytest.mark.parametrize(("start", "now", "net", "kind"), [
    ("sandbox_full", "sandbox_full", "web", "note"),   # the only notes-only cell
    ("sandbox_open", "sandbox_open", "web", "breach"),
    ("sandbox_full", "sandbox_full", "base", "breach"),  # strict or base job in sandbox_full: holds
    ("sandbox_full", "sandbox_open", "web", "breach"),  # narrowing mid-job restores the hold at once
    ("sandbox_open", "sandbox_full", "web", "breach"),  # widening never relaxes a running job
    ("allowlist", "allowlist", "base", "breach"),
    ("open", "open", "base", "breach"),
])  # fmt: skip
def test_volume_breach_is_a_note_only_for_web_jobs_of_sandbox_full(tmp_path: Path, start: str, now: str, net: str,
                                                                   kind: str) -> None:  # fmt: skip
    rig = Rig(tmp_path, mode=now)
    rig.job(mode=start, net=net)
    _over_volume(rig)
    rig.meter.poll()
    rig.meter.poll()  # once per job
    other = "breach" if kind == "note" else "note"
    assert rig.runs(kind) == ["r1"] and rig.runs(other) == [] and rig.runs("floor") == []
    if kind == "note":
        assert rig.events["note"][0][1]["kind"] == "volume" and "hosts" in rig.events["note"][0][1]["breached"]
    doc = rig.meter.finish("r1")
    assert doc is not None and doc["policy"] == ("notes" if (start, net) == ("sandbox_full", "web") else "hold")


def test_an_unreadable_mode_holds(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    def boom() -> str:
        raise OSError("flag unreadable")

    rig.meter.current_mode = boom
    rig.job()
    _over_volume(rig)
    rig.meter.poll()
    assert rig.runs("breach") == ["r1"]


# ------------------------------------------------------------------------------------- squid floor marks
@pytest.mark.parametrize("mode", ["allowlist", "sandbox_open", "sandbox_full"])
@pytest.mark.parametrize(("mark", "host", "net", "cls"), [
    ("dst", "repo.maven.apache.org", "web", "dst"), ("dst", "repo.maven.apache.org", "base", "dst"),
    ("name", "uploads.github.com", "base", "name"), ("code", "api.telegram.org", "web", "code"),
    ("literal", "10.0.0.1", "base", "literal"), ("literal", "0x7f.1", "web", None),  # loopback literal: a note
])  # fmt: skip
def test_floor_marks_hold_in_every_mode(tmp_path: Path, mode: str, mark: str, host: str, net: str,
                                        cls: str | None) -> None:  # fmt: skip
    rig = Rig(tmp_path, mode=mode)
    rig.job(mode=mode, net=net)
    rig.write_squid(sq(T0 + 1, "10.213.0.18", host, result="TCP_DENIED/403", server="-", mark=mark))
    rig.meter.poll()
    rig.write_squid(sq(T0 + 2, "10.213.0.18", host, result="TCP_DENIED/403", server="-", mark=mark))
    rig.meter.poll()
    if cls is None:
        assert rig.runs("floor") == []
        return
    assert rig.runs("floor") == ["r1"]  # once per job
    assert rig.events["floor"][0][1]["floor"]["class"] == cls and rig.runs("breach") == []


@pytest.mark.parametrize(("mark", "host", "net"), [
    ("dst_loop", "localtest.me", "web"), ("quiet", "checkpoint-api.hashicorp.com", "web"),
    ("code", "api.telegram.org", "base"), ("literal", "127.0.0.1", "base"), ("literal", "[::1]", "web"),
    ("literal", "8.8.8.8", "web"),  # a public literal: an ordinary deny, not even a note
])  # fmt: skip
def test_notes_and_ordinary_denies_never_hold(tmp_path: Path, mark: str, host: str, net: str) -> None:
    rig = Rig(tmp_path)
    rig.job(net=net)
    rig.write_squid(sq(T0 + 1, "10.213.0.18", host, result="TCP_DENIED/403", server="-", mark=mark))
    rig.meter.poll()
    doc = rig.meter.finish("r1")
    assert rig.runs("floor") == [] and rig.runs("breach") == [] and doc is not None
    if host == "8.8.8.8":
        assert rig.runs("note") == [] and doc["notes"] == []
    else:
        assert rig.runs("note") == ["r1"] and rig.events["note"][0][1]["kind"] == "floor_note"


def test_a_floor_server_address_without_a_mark_holds(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.job()
    rig.write_squid(sq(T0 + 1, "10.213.0.18", "rebind.example", server="10.1.2.3"))
    rig.meter.poll()
    assert rig.runs("floor") == ["r1"] and rig.events["floor"][0][1]["floor"]["class"] == "dst"
    rig2 = Rig(tmp_path / "b")
    rig2.job()
    rig2.write_squid(sq(T0 + 1, "10.213.0.18", "fine.example", server="-"))
    rig2.meter.poll()
    assert rig2.runs("floor") == []


# --------------------------------------------------------------------------------------- kernel lines
def test_in_line_holds_the_job_of_its_28_within_the_window(tmp_path: Path) -> None:
    rig = Rig(tmp_path, mode="allowlist")
    rig.job(mode="allowlist", net="base")
    rig.job("r2", CID2, NET2, mode="allowlist", net="base")
    rig.write_floor(fl(T0 + 5, "in", "10.213.0.18", "10.213.0.17", "UDP", 53))
    rig.meter.poll()
    assert rig.runs("floor") == ["r1"] and rig.events["floor"][0][1]["floor"]["class"] == "host"


def test_in_line_after_finish_plus_3s_is_not_attributed(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.job()
    rig.meter.finish("r1")  # finish_ts = now
    fin = rig.now[0]
    rig.job("r2", CID2, start=fin + 10)  # the /28 is reused by the next job
    rig.write_floor(fl(fin + 4, "in", "10.213.0.18", "10.213.0.17"))
    rig.meter.poll()
    assert rig.runs("floor") == []


def test_a_late_in_line_within_30s_holds_the_finished_job(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.job()
    rig.meter.finish("r1")
    fin = rig.now[0]
    rig.now[0] = fin + 20  # journal latency: written later, stamped inside the window
    rig.write_floor(fl(fin + 2, "in", "10.213.0.18", "10.213.0.17"))
    rig.meter.poll()
    assert rig.runs("floor") == ["r1"]
    rig.now[0] = fin + 40  # gone from the recently-finished list
    rig.write_floor(fl(fin + 2, "in", "10.213.0.19", "10.213.0.17"))
    rig.meter.poll()
    assert rig.runs("floor") == ["r1"]


@pytest.mark.parametrize("dst", ["192.168.1.1", "10.212.0.1", "172.21.195.2"])  # forwarded and distro-local
def test_px_from_the_live_proxy_holds_every_running_metered_job(tmp_path: Path, dst: str) -> None:
    rig = Rig(tmp_path, mode="allowlist")
    rig.job(mode="allowlist", net="base")
    rig.job("r2", CID2, NET2, mode="allowlist", net="base", stage="fetch")
    rig.write_floor(fl(T0 + 5, "px", "10.212.0.2", dst))
    rig.meter.poll()
    assert sorted(rig.runs("floor")) == ["r1", "r2"] and rig.notices == []
    assert {d["floor"]["class"] for _, d in rig.events["floor"]} == {"proxy"}


def test_px_with_no_job_sends_one_notice(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    rig = Rig(tmp_path)
    with caplog.at_level(logging.ERROR):
        rig.write_floor(fl(T0 + 5, "px", "10.212.0.2", "192.168.1.1"), fl(T0 + 6, "px", "10.212.0.2", "192.168.1.1"))
        rig.meter.poll()
    assert rig.notices == ["Proxy floor drop to 192.168.1.1:443 with no job running"]  # deduped per dst
    assert any(getattr(r, "event", "") == "proxy_floor_unattributed" for r in caplog.records)
    assert rig.runs("floor") == []


@pytest.mark.parametrize("line", [fl(T0 + 5, "px", "10.212.0.245", "192.168.1.1"),
                                  fl(T0 + 5, "tx", "10.212.0.241", "10.0.0.1"),
                                  fl(T0 + 5, "tx", "10.212.0.2", "10.0.0.1")])  # fmt: skip
def test_test_proxy_lines_never_hold_or_notify(tmp_path: Path, line: str) -> None:
    rig = Rig(tmp_path)
    rig.job()
    rig.write_floor(line)
    rig.meter.poll()
    assert rig.runs("floor") == [] and rig.notices == [] and rig.meter.health()["meter"]["px_other"] == 1


def test_malformed_floor_lines_are_counted(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.job()
    rig.write_floor("garbage\n", fl(T0 + 5, "in", "fd00::1", "10.0.0.1"), "1.000 zz 10.213.0.18 10.0.0.1 TCP 1\n")
    rig.meter.poll()
    assert rig.meter.health()["meter"]["floor_malformed"] == 3 and rig.runs("floor") == []


def test_heartbeat_older_than_120s_is_down_with_one_notice(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.meter.poll()
    assert rig.meter.health()["floor_detector"] == "ok"
    rig.now[0] += 121
    rig.meter.poll()
    rig.meter.poll()
    assert rig.meter.health()["floor_detector"] == "down"
    assert len(rig.notices) == 1 and rig.notices[0].startswith("Floor detector down")
    rig.write_floor(hb(rig.now[0]))
    rig.meter.poll()
    assert rig.meter.health()["floor_detector"] == "ok"


# ------------------------------------------------------------------------- late-check ring buffer (C-N5)
def test_ring_buffer_evicts_oldest_first_and_counts(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    rig = Rig(tmp_path, ring_max=3)
    rig.job()
    rig.job("r2", CID2, NET2)
    with caplog.at_level(logging.INFO):
        rig.write_squid(sq(T0 + 1, "10.213.0.18", "a.example"), sq(T0 + 2, "10.213.0.18", "b.example"),
                        sq(T0 + 3, "10.213.0.34", "c.example"), sq(T0 + 4, "10.213.0.34", "d.example"),
                        sq(T0 + 5, "10.213.0.34", "e.example"))  # fmt: skip
        rig.meter.poll()
    ring = list(rig.meter._ring)
    assert [e.ts for e in ring] == [T0 + 3, T0 + 4, T0 + 5]  # the two oldest went first
    assert rig.meter.health()["meter"]["late_check_evicted"] == 2
    lost = [r for r in caplog.records if getattr(r, "event", "") == "late_check_evicted"]
    assert [getattr(r, "run_id", "") for r in lost] == ["r1"]  # one INFO line per job whose window lost entries
    assert rig.meter._jobs["r2"].ring_lost is False


def test_ring_buffer_drops_entries_older_than_10_minutes_uncounted(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.job()
    rig.now[0] = T0 + 1000
    rig.write_squid(sq(T0 + 1, "10.213.0.18", "old.example"))
    rig.meter.poll()
    rig.write_squid(sq(T0 + 999, "10.213.0.18", "new.example"))
    rig.meter.poll()
    assert [e.ts for e in rig.meter._ring] == [T0 + 999]
    assert rig.meter.health()["meter"]["late_check_evicted"] == 0


def test_floors_from_the_live_squid_conf(tmp_path: Path) -> None:
    conf = tmp_path / "squid.conf"
    conf.write_text('acl denied_dst_loop dst 127.0.0.0/8 0.0.0.0/8 ::1 ::\n'
                    'acl denied_dst dst 10.0.0.0/8 203.0.113.9/32 fd00::/8\n'
                    'acl denied_dst dst "/etc/squid/floor/public.txt"\n', encoding="utf-8")  # fmt: skip
    floors = floors_from_squid_conf(conf)
    assert isinstance(floors, Floors)
    assert floors.is_floor(ipaddress.ip_address("203.0.113.9")) and floors.is_floor(ipaddress.ip_address("10.9.9.9"))
    missing = floors_from_squid_conf(tmp_path / "absent.conf")  # the built-ins only, never weaker
    assert missing.is_floor(ipaddress.ip_address("192.168.0.1")) and not missing.is_floor(
        ipaddress.ip_address("203.0.113.9"))  # fmt: skip
