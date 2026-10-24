"""Per-job egress meter of repo-code jobs (open-discovery design §13.2, ADR-0023; build-env design §4.5, §4.7,
§5.1, ADR-0025): the volume guard and the floor alarm.

One thread polls the squid access log (``/srv/ossbot/logs/proxy/access.log``) every 10 s, reading at most
8 MiB per poll from a held file descriptor. Rotation (``ossbot-proxy-rotate`` renames the file, then squid
reopens it): when the path's inode differs from the held fd's, or the path is missing, the held fd is read
to its end first and then closed, and the path is opened once it exists, so no line is lost across the
rename. The first open at start skips what is already there (no job can own it: a restart interrupts every
job). Lines are attributed to registered jobs by ``common.egress_log`` (client /28, listener port, time).

**Volume** (``egress.anomaly``): a job over ``max_hosts`` / ``max_upload_mb`` / ``max_download_mb`` (MiB) is
reported once through ``on_breach`` (``JobService.flag_anomaly``: stop and hold), on any poll or at
``finish`` (one last poll after a 3 s grace: tunnels close when the containers stop and are logged then) —
unless the job is a web job of ``sandbox_full`` (the mode at its start and now, ``net == web``): then the
breach is a note (``on_note``, once per job) and the job runs on (§2.3). A narrowing switch restores holds
at once; widening never relaxes a running job.

**Floors**, in every mode, for every metered job (strict ones too): each squid line is classified
(``floor_class``: the marks of squid's deny lines, decoded literals, the supplementary server address); a
``hold`` class stops the job and holds the contribution once (``on_floor``), a ``note`` class is noted. A second
reader follows the kernel floor log of ``ossbot-floorlog`` (``/srv/ossbot/logs/floor/floor.log``): ``in`` lines
(a sandbox packet at a host address) hold the job whose /28 sent it (start - 1 s .. finish + 3 s); ``px`` lines
(the live proxy 10.212.0.2's connection to a floor, dropped by the kernel: a DNS rebinding past squid) hold
every metered job running then, and with none running send one P1 notice per destination (1 h) and ERROR
``proxy_floor_unattributed``; ``tx`` lines (root's test proxies) never hold or notify (``px_other``). Finished
jobs stay attributable for 30 s (journal latency). A heartbeat older than 120 s is ``floor_detector: down``
(one P1 notice per hour); it gates nothing: the kernel drops on its own.

**Public-address floor** (§4.7): ``state.json``/``public.txt`` are re-read when their ``generation`` changes;
a newly learned address triggers a late check over a ring buffer of the last 10 minutes of successful squid
connections (at most 50 000 entries, oldest first out: ``late_check_evicted`` counts entries lost to the cap,
with one INFO line per job whose window lost some): a connection whose server address is the new one is a
``pub`` floor hit and holds that contribution. While the floor is unknown, running web jobs are stopped with
``public_floor_unknown`` (no hold, no note; the runner defers them) and, in a web mode, a P1 notice goes out
after 10 minutes (6 h). The address never appears in a log, summary, hold or notice ("public-address").

Every job's summary is written to ``<logs>/runs/<run_id>.egress.json`` (0640) and logged at INFO
``egress_usage``. The same thread calls ``retry`` (the pending hold requests) every 30 s and logs the
counters then (the heartbeat summary).
"""

from __future__ import annotations

import collections
import ipaddress
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from ossbot.common.config import EgressAnomaly
from ossbot.common.control_flags import CODE_WEB_MODES
from ossbot.common.egress_log import (
    LOOP_CIDRS,
    FloorHit,
    Floors,
    IPNetwork,
    JobWindow,
    Usage,
    floor_class,
    parse_ip,
    parse_line,
)
from ossbot.common.public_floor import FloorStatus, read_public, read_state

log = logging.getLogger(__name__)
POLL_S = 10.0
RETRY_S = 30.0
MAX_READ = 8 * 1024 * 1024
GRACE_S = 3.0
MIB = 1024 * 1024
PROXY_IP = "10.212.0.2"  # ctl/render.py PROXY_IP: the only source whose kernel floor lines hold
RECENT_S = 30.0  # finished jobs stay attributable for late floor-log lines
FINISH_SLACK_S = 3.0  # an ``in``/``px`` line up to finish + 3 s still belongs to the job (N4)
HEARTBEAT_MAX_S = 120.0
DETECTOR_NOTICE_S = 3600.0
UNATTRIBUTED_NOTICE_S = 3600.0
UNKNOWN_NOTICE_AFTER_S = 600.0
UNKNOWN_NOTICE_S = 6 * 3600.0
RING_MAX = 50_000
RING_S = 600.0
HITS_MAX = 20
NOTES_MAX = 20
PUBLIC_TARGET = "public-address"
FLOOR_LINE_RE = re.compile(r"^(\d+\.\d{3}) (in|px|tx) (\S+) (\S+) (TCP|UDP) (\d{1,5})$")
HEARTBEAT_RE = re.compile(r"^(\d+\.\d{3}) hb - - HB 0$")


@dataclass
class Job:
    run_id: str
    cid: str
    stage: str
    subnet: str
    egress: str
    egress_mode: str
    limits: EgressAnomaly
    window: JobWindow
    usage: Usage = field(default_factory=Usage)
    flagged: bool = False
    net: str = "base"  # web: base + web_public (the volume guard may be a note); base: base list only
    finish_ts: float | None = None
    floor_hits: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    floor_flagged: bool = False
    noted: bool = False
    unknown_stopped: bool = False
    ring_lost: bool = False

    def in_window(self, ts: float) -> bool:
        end = float("inf") if self.finish_ts is None else self.finish_ts + FINISH_SLACK_S
        return self.window.start_ts - 1 <= ts <= end


def breached(usage: Usage, limits: EgressAnomaly) -> list[str]:
    out = []
    if len(usage.hosts) > limits.max_hosts:
        out.append("hosts")
    if usage.up > limits.max_upload_mb * MIB:
        out.append("upload")
    if usage.down > limits.max_download_mb * MIB:
        out.append("download")
    return out


def _is_ip(value: str) -> bool:
    return parse_ip(value) is not None


def scrub(hit: FloorHit) -> FloorHit:
    """§4.7 privacy: a public-floor value never leaves sandboxd (``public-address``, ``dst -``)."""
    if not hit.public and hit.cls != "pub":
        return hit
    target = PUBLIC_TARGET if _is_ip(hit.target) else hit.target
    return FloorHit(hit.cls, hit.policy, target, "-", True)


def scrub_hosts(hosts: list[str], public: tuple[IPNetwork, ...]) -> list[str]:
    """§4.7 privacy: a host that is a literal of the public floor is ``public-address`` in every summary."""
    out = []
    for host in hosts:
        ip = parse_ip(host)
        out.append(PUBLIC_TARGET if ip is not None and Floors((), (), public).is_public(ip) else host)
    return out


def summary(job: Job, end_ts: float | None = None, public: tuple[IPNetwork, ...] = ()) -> dict[str, Any]:
    usage = job.usage.as_dict()
    usage["top_hosts"] = scrub_hosts(usage["top_hosts"], public)
    return {"run_id": job.run_id, "cid": job.cid, "stage": job.stage, "egress": job.egress,
            "egress_mode": job.egress_mode, "subnet": job.subnet,
            "window": {"start": job.window.start_ts, "end": end_ts}, **usage,
            "limits": job.limits.model_dump(), "breached": breached(job.usage, job.limits), "net": job.net,
            "floor_hits": list(job.floor_hits), "notes": list(job.notes),
            "policy": "notes" if job.net == "web" and job.egress_mode == "sandbox_full" else "hold"}  # fmt: skip


def floors_from_squid_conf(conf: Path) -> Floors:
    """The floors squid enforces: the inline ``acl denied_dst dst`` values of the live squid.conf (every
    non-loopback floor range and the live host addresses; the built-ins are unioned anyway). Unreadable:
    the built-ins only."""
    try:
        text = conf.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return Floors.of([])
    for line in text.splitlines():
        if line.startswith("acl denied_dst dst ") and '"' not in line:
            values = []
            for v in line.split()[3:]:
                try:
                    values.append(str(ipaddress.ip_network(v, strict=False)))
                except ValueError:
                    continue
            return Floors.of([v for v in values if v not in LOOP_CIDRS])
    return Floors.of([])


class LineReader:
    """Complete lines of a log written by another process, from a held fd, rename-safe (one class, two logs)."""

    def __init__(self, path: Path, max_read: int) -> None:
        self.path = path
        self.max_read = max_read
        self._fh: IO[bytes] | None = None
        self._tail = b""
        self._first = True

    def _open(self, *, at_end: bool) -> None:
        try:
            fh = open(self.path, "rb")  # noqa: SIM115 - held across polls, closed on rotation
        except OSError:
            return
        if at_end:
            fh.seek(0, os.SEEK_END)
        self._fh = fh

    def _rotated(self) -> bool:
        assert self._fh is not None
        try:
            return os.stat(self.path).st_ino != os.fstat(self._fh.fileno()).st_ino
        except FileNotFoundError:
            return True

    def prime(self) -> None:
        if self._fh is None and self._first:
            self._open(at_end=True)
            self._first = False

    def read(self) -> list[bytes]:
        """Complete lines read in this poll (≤ ``max_read`` bytes)."""
        if self._fh is None:
            self._open(at_end=self._first)  # only the file found at start is skipped; a later one is new
            self._first = False
            if self._fh is None:
                return []
        data = self._fh.read(self.max_read)
        lines: list[bytes] = []
        if data:
            chunk = self._tail + data
            *done, self._tail = chunk.split(b"\n")
            lines.extend(done)
        if len(data) < self.max_read and self._rotated():  # at the held file's end and it was renamed
            if self._tail:
                lines.append(self._tail)
                self._tail = b""
            self._fh.close()
            self._fh = None
            self._open(at_end=False)  # the new file from its start (nothing is lost)
        return lines


@dataclass(frozen=True)
class RingEntry:
    ts: float
    job: Job
    server: str


Report = tuple[str, str, dict[str, Any]]  # (kind, run_id, doc): breach | floor | note | unknown


class EgressMeter:
    def __init__(
        self,
        path: Path,
        summaries_dir: Path,
        limits: Callable[[], EgressAnomaly],
        *,
        on_breach: Callable[[str, dict[str, Any]], None] = lambda run_id, s: None,
        retry: Callable[[], object] = lambda: None,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        grace_s: float = GRACE_S,
        max_read: int = MAX_READ,
        floor_log: Path | None = None,
        floors: Callable[[], Floors] | None = None,
        public_floor_dir: Path | None = None,
        current_mode: Callable[[], str] = lambda: "allowlist",
        on_floor: Callable[[str, dict[str, Any]], None] = lambda run_id, s: None,
        on_note: Callable[[str, dict[str, Any]], None] = lambda run_id, s: None,
        on_unknown: Callable[[str, dict[str, Any]], None] = lambda run_id, s: None,
        notify: Callable[[str], None] = lambda text: None,
        ring_max: int = RING_MAX,
    ) -> None:
        self.path = path
        self.summaries_dir = summaries_dir
        self.limits = limits
        self.on_breach = on_breach
        self.retry = retry
        self.wall = wall
        self.sleep = sleep
        self.grace_s = grace_s
        self.max_read = max_read
        self.floors = floors or (lambda: Floors.of([]))
        self.public_floor_dir = public_floor_dir
        self.current_mode = current_mode
        self.on_floor = on_floor
        self.on_note = on_note
        self.on_unknown = on_unknown
        self.notify = notify
        self.malformed = 0
        self.counters: collections.Counter[str] = collections.Counter()
        self._jobs: dict[str, Job] = {}
        self._recent: dict[str, Job] = {}
        self._squid = LineReader(path, max_read)
        self._floor = LineReader(floor_log, max_read) if floor_log is not None else None
        self._started = wall()
        self._last_hb: float | None = None
        self._notified: dict[str, float] = {}
        self._public: tuple[IPNetwork, ...] = ()
        self._public_gen: int | None = None
        self._public_status = FloorStatus(False, None, 0, 0, False)
        self._unknown_seen: float | None = None
        self._ring: collections.deque[RingEntry] = collections.deque()
        self._ring_max = ring_max
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ jobs
    def register(self, run_id: str, cid: str, stage: str, subnet: str, start_ts: float, egress: str,
                 egress_mode: str, net: str = "base") -> None:  # fmt: skip
        try:
            limits = self.limits()
        except Exception:  # noqa: BLE001 - an unreadable snapshot must not stop the job: schema defaults
            log.warning("egress limits unreadable; using the defaults", extra={"event": "egress_limits_default"})
            limits = EgressAnomaly()
        with self._lock:
            self._jobs[run_id] = Job(run_id, cid, stage, subnet, egress, egress_mode, limits,
                                     JobWindow.of(subnet, start_ts), net="web" if net == "web" else "base")  # fmt: skip

    def set_net(self, run_id: str, net: str) -> None:
        """The job's network as admission decided it (``web`` or ``base``); anything else is ``base``."""
        with self._lock:
            job = self._jobs.get(run_id)
            if job is not None:
                job.net = "web" if net == "web" else "base"

    def finish(self, run_id: str) -> dict[str, Any] | None:
        """After the job's containers stopped: grace, last poll, final breach check, summary file."""
        if run_id not in self._jobs:
            return None
        self.sleep(self.grace_s)
        self.poll()
        reports: list[Report] = []
        with self._lock:
            job = self._jobs.pop(run_id, None)
            if job is None:
                return None
            job.finish_ts = self.wall()
            self._recent[run_id] = job  # late floor-log lines still hold it for RECENT_S
            self._volume(job, reports)
            if job.notes and not job.noted and not job.floor_flagged:
                job.noted = True
                reports.append(("note", run_id, {**summary(job, public=self._public), "kind": "floor_note"}))
            doc = summary(job, job.finish_ts, self._public)
        self._dispatch(reports)
        self._write(doc)
        log.info("egress usage of %s: %d hosts, %d B up, %d B down, %d denied", run_id, doc["hosts"],
                 doc["up_bytes"], doc["down_bytes"], doc["denied"],
                 extra={"event": "egress_usage", "run_id": run_id, "cid": job.cid})  # fmt: skip
        return doc

    def _notes_only(self, job: Job) -> bool:
        """§2.3: volume is a note only for a web job of sandbox_full, at its start and now."""
        if job.net != "web" or job.egress_mode != "sandbox_full":
            return False
        try:
            return self.current_mode() == "sandbox_full"
        except Exception:  # noqa: BLE001 - unreadable mode: hold (fail closed)
            return False

    def _volume(self, job: Job, reports: list[Report]) -> None:
        if job.flagged or not breached(job.usage, job.limits):
            return
        if self._notes_only(job):
            if not job.noted:
                job.noted = True
                reports.append(("note", job.run_id, {**summary(job, public=self._public), "kind": "volume"}))
            return
        job.flagged = True
        reports.append(("breach", job.run_id, summary(job, public=self._public)))

    def _hit(self, job: Job, hit: FloorHit, ts: float, reports: list[Report]) -> None:
        hit = scrub(hit)
        if hit.policy == "note":
            if len(job.notes) < NOTES_MAX:
                job.notes.append(f"{hit.cls}: {hit.target}"[:120])
            return
        if len(job.floor_hits) < HITS_MAX:
            job.floor_hits.append({"class": hit.cls, "host": hit.target, "ts": ts})
        if not job.floor_flagged:
            job.floor_flagged = True
            floor = {"class": hit.cls, "target": hit.target, "dst": hit.dst}
            reports.append(("floor", job.run_id, {**summary(job, public=self._public), "floor": floor}))

    def _dispatch(self, reports: list[Report]) -> None:
        handlers = {"breach": self._report, "floor": self._report_floor, "note": self._report_note,
                    "unknown": self._report_unknown}  # fmt: skip
        for kind, run_id, doc in reports:  # outside the lock: the handlers kill containers and call the gatekeeper
            handlers[kind](run_id, doc)

    def _report(self, run_id: str, doc: dict[str, Any]) -> None:
        log.warning("egress anomaly in %s: %s (%d hosts, %d B up, %d B down)", run_id, ",".join(doc["breached"]),
                    doc["hosts"], doc["up_bytes"], doc["down_bytes"],
                    extra={"event": "egress_anomaly", "run_id": run_id, "cid": doc["cid"]})  # fmt: skip
        try:
            self.on_breach(run_id, doc)
        except Exception:
            log.exception("egress anomaly handling failed for %s", run_id, extra={"event": "egress_anomaly_failed"})

    def _report_floor(self, run_id: str, doc: dict[str, Any]) -> None:
        floor = doc["floor"]
        log.warning("floor reach attempt in %s: %s %s", run_id, floor["class"], floor["target"],
                    extra={"event": "egress_floor", "run_id": run_id, "cid": doc["cid"]})  # fmt: skip
        try:
            self.on_floor(run_id, doc)
        except Exception:
            log.exception("floor hold handling failed for %s", run_id, extra={"event": "egress_floor_failed"})

    def _report_note(self, run_id: str, doc: dict[str, Any]) -> None:
        log.info("egress note for %s (%s): %s", run_id, doc.get("kind"), ",".join(doc["breached"]) or "floor notes",
                 extra={"event": "egress_note", "run_id": run_id, "cid": doc["cid"]})  # fmt: skip
        try:
            self.on_note(run_id, doc)
        except Exception:
            log.exception("egress note handling failed for %s", run_id, extra={"event": "egress_note_failed"})

    def _report_unknown(self, run_id: str, doc: dict[str, Any]) -> None:
        log.warning("public floor unknown: stopping web job %s (deferred, not failed)", run_id,
                    extra={"event": "public_floor_unknown", "run_id": run_id, "cid": doc["cid"]})  # fmt: skip
        try:
            self.on_unknown(run_id, doc)
        except Exception:
            log.exception("public floor stop failed for %s", run_id, extra={"event": "public_floor_stop_failed"})

    def _notice(self, key: str, text: str, every_s: float) -> None:
        now = self.wall()
        last = self._notified.get(key)
        if last is not None and now - last < every_s:
            return
        self._notified[key] = now
        try:
            self.notify(text)
        except Exception:  # noqa: BLE001 - a notice is best effort (WARNING notify_dropped)
            log.warning("notice dropped (%s)", key, extra={"event": "notify_dropped"})

    def _write(self, doc: dict[str, Any]) -> None:
        try:
            self.summaries_dir.mkdir(parents=True, exist_ok=True)
            path = self.summaries_dir / f"{doc['run_id']}.egress.json"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, sort_keys=True)
            os.chmod(path, 0o640)
        except OSError as exc:
            log.warning("egress summary not written: %s", exc, extra={"event": "egress_summary_failed"})

    # ------------------------------------------------------------------ polling
    def _floors_now(self) -> Floors:
        try:
            base = self.floors()
        except Exception:  # noqa: BLE001 - never weaker than the built-ins
            base = Floors.of([])
        return Floors(base.cidrs, base.loop, self._public)

    def _ring_add(self, job: Job, ts: float, server: str) -> None:
        cutoff = self.wall() - RING_S
        while self._ring and self._ring[0].ts < cutoff:
            self._ring.popleft()  # aged out of the 10-minute window: expected
        while len(self._ring) >= self._ring_max:
            lost = self._ring.popleft()  # the cap: oldest first out, counted
            self.counters["late_check_evicted"] += 1
            if not lost.job.ring_lost:
                lost.job.ring_lost = True
                log.info("late public-floor check: %s lost buffered entries of its window (cap %d)",
                         lost.job.run_id, self._ring_max,
                         extra={"event": "late_check_evicted", "run_id": lost.job.run_id})  # fmt: skip
        self._ring.append(RingEntry(ts, job, server))

    def _public_floor(self, reports: list[Report]) -> None:
        if self.public_floor_dir is None:
            return
        now = self.wall()
        status = read_state(self.public_floor_dir, now)
        self._public_status = status
        if status.generation != self._public_gen or not self._public:
            try:
                nets = tuple(ipaddress.ip_network(n) for n in read_public(self.public_floor_dir))
            except (OSError, ValueError):
                log.error("public floor file unreadable or invalid; keeping the last good copy",
                          extra={"event": "public_floor_file_invalid"})  # fmt: skip
                nets = self._public
            added = [n for n in nets if n not in self._public]
            first = self._public_gen is None
            self._public, self._public_gen = nets, status.generation
            if added and not first:
                self._late_check(added, reports)
        if status.known:
            self._unknown_seen = None
            return
        self._unknown_seen = self._unknown_seen or now
        for job in self._jobs.values():
            if job.net == "web" and not job.unknown_stopped:
                job.unknown_stopped = True
                reports.append(("unknown", job.run_id, summary(job, public=self._public)))
        since = status.since or self._unknown_seen
        try:
            web_mode = self.current_mode() in CODE_WEB_MODES
        except Exception:  # noqa: BLE001
            web_mode = False
        if web_mode and now - since >= UNKNOWN_NOTICE_AFTER_S:
            self._notice("public_floor_unknown", "Public-address check failing for 10 min: repo code's public web is"
                         " paused", UNKNOWN_NOTICE_S)  # fmt: skip

    def _late_check(self, added: list[IPNetwork], reports: list[Report]) -> None:
        public = Floors((), (), tuple(added))
        for entry in self._ring:
            ip = parse_ip(entry.server)
            if ip is not None and public.is_public(ip):
                hit = FloorHit("pub", "hold", PUBLIC_TARGET, "-", True)
                self._hit(entry.job, hit, entry.ts, reports)

    def _floor_line(self, raw: str, floors: Floors, reports: list[Report]) -> None:
        hb = HEARTBEAT_RE.fullmatch(raw)
        if hb is not None:
            self._last_hb = float(hb.group(1))
            return
        m = FLOOR_LINE_RE.fullmatch(raw)
        if m is None:
            self.counters["floor_malformed"] += 1
            return
        ts, (kind, src, dst, _proto, dport) = float(m.group(1)), m.group(2, 3, 4, 5, 6)
        try:
            src_ip, dst_ip = ipaddress.IPv4Address(src), ipaddress.IPv4Address(dst)
        except ValueError:
            self.counters["floor_malformed"] += 1
            return
        candidates = [*self._jobs.values(), *self._recent.values()]
        if kind == "in":
            for job in candidates:
                if src_ip in job.window.subnet and job.in_window(ts):
                    self._hit(job, FloorHit("host", "hold", str(dst_ip), str(dst_ip)), ts, reports)
            return
        if kind == "tx" or src != PROXY_IP:  # a root test proxy (or a PFLOOR line floorlog demoted)
            self.counters["px_other"] += 1
            log.info("kernel floor line of a test proxy (never holds)", extra={"event": "floor_test_line"})
            return
        public = floors.is_public(dst_ip)
        hit = FloorHit("pub" if public else "proxy", "hold", str(dst_ip), str(dst_ip), public)
        running = [job for job in candidates if job.in_window(ts)]
        for job in running:  # the source is squid: every metered job running then (over-holding is safe)
            self._hit(job, hit, ts, reports)
        if not running:
            shown = scrub(hit)
            where = "your public address" if shown.public else shown.target
            log.error("proxy floor drop to %s:%s with no job running", where, dport,
                      extra={"event": "proxy_floor_unattributed"})  # fmt: skip
            self._notice(f"px:{shown.target}:{dport}", f"Proxy floor drop to {where}:{dport} with no job running",
                         UNATTRIBUTED_NOTICE_S)  # fmt: skip

    def _detector(self) -> None:
        if self._floor is None:
            return
        now = self.wall()
        last = self._last_hb if self._last_hb is not None else self._started
        if now - last > HEARTBEAT_MAX_S:
            self._notice("floor_detector", "Floor detector down: kernel floor drops are not reported (the drops"
                         " still block)", DETECTOR_NOTICE_S)  # fmt: skip

    def poll(self) -> None:
        reports: list[Report] = []
        with self._lock:
            self._public_floor(reports)
            floors = self._floors_now()
            for raw in self._squid.read():
                text = raw.decode("utf-8", "replace")
                if not text.strip():
                    continue
                e = parse_line(text)
                if e is None:
                    self.malformed += 1
                    continue
                for job in self._jobs.values():
                    if job.window.matches(e):
                        job.usage.add(e)
                        if not e.denied and e.server not in ("-", ""):
                            self._ring_add(job, e.ts, e.server)
                        hit = floor_class(e, floors, "web" if job.net == "web" else "base")
                        if hit is not None:
                            self._hit(job, hit, e.ts, reports)
            if self._floor is not None:
                for raw in self._floor.read():
                    text = raw.decode("utf-8", "replace").strip()
                    if text:
                        self._floor_line(text, floors, reports)
            now = self.wall()
            expired = [r for r, j in self._recent.items() if j.finish_ts is not None and now - j.finish_ts > RECENT_S]
            for run_id in expired:
                del self._recent[run_id]
            for job in self._jobs.values():
                self._volume(job, reports)
            self._detector()
        self._dispatch(reports)

    def health(self) -> dict[str, Any]:
        """``/v1/health`` fields: the floor detector and the public floor (a count, never the values)."""
        with self._lock:
            now = self.wall()
            if self._floor is None:
                detector = "off"
            else:
                last = self._last_hb if self._last_hb is not None else self._started
                detector = "ok" if now - last <= HEARTBEAT_MAX_S else "down"
            status = self._public_status
            counters = {"px_other": self.counters["px_other"],
                        "late_check_evicted": self.counters["late_check_evicted"],
                        "malformed": self.malformed, "floor_malformed": self.counters["floor_malformed"]}  # fmt: skip
            return {"floor_detector": detector, "public_floor": status.as_dict(), "meter": counters}

    def heartbeat(self) -> None:
        """The periodic summary of the counters (every 30 s, with the hold retries)."""
        h = self.health()
        log.info("egress meter: floor detector %s, public floor known=%s (%d entries), %s", h["floor_detector"],
                 h["public_floor"]["known"], h["public_floor"]["entries"],
                 ", ".join(f"{k} {v}" for k, v in h["meter"].items()),
                 extra={"event": "egress_meter_heartbeat", **h["meter"]})  # fmt: skip

    # ------------------------------------------------------------------ thread
    def prime(self) -> None:
        """Open the logs at their current end (sandboxd start, before any job can be registered)."""
        with self._lock:
            self._squid.prime()
            if self._floor is not None:
                self._floor.prime()

    def start(self, poll_s: float = POLL_S, retry_s: float = RETRY_S) -> None:
        self.prime()

        def loop() -> None:
            last_retry = 0.0
            while not self._stop.wait(poll_s):
                try:
                    self.poll()
                except Exception:
                    log.exception("egress meter poll failed", extra={"event": "egress_meter_failed"})
                if time.monotonic() - last_retry >= retry_s:
                    last_retry = time.monotonic()
                    try:
                        self.retry()
                    except Exception:
                        log.exception("hold retry failed", extra={"event": "hold_retry_failed"})
                    try:
                        self.heartbeat()
                    except Exception:
                        log.exception("egress meter heartbeat failed", extra={"event": "egress_meter_failed"})

        self._thread = threading.Thread(target=loop, daemon=True, name="egress-meter")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
