"""Per-job egress meter of fetch/validate jobs (open-discovery design §13.2, ADR-0023): the ``sandbox_open``
safety net. Also strict jobs are metered (their numbers tune the limits).

One thread polls the squid access log (``/srv/ossbot/logs/proxy/access.log``) every 10 s, reading at most
8 MiB per poll from a held file descriptor. Rotation (``ossbot-proxy-rotate`` renames the file, then squid
reopens it): when the path's inode differs from the held fd's, or the path is missing, the held fd is read
to its end first and then closed, and the path is opened once it exists, so no line is lost across the
rename. The first open at start skips what is already there (no job can own it: a restart interrupts every
job). Lines are attributed to registered jobs by ``common.egress_log`` (client /28, listener port, time).

A job over ``max_hosts`` / ``max_upload_mb`` / ``max_download_mb`` (MiB) is reported once through
``on_breach`` (``JobService.flag_anomaly``), on any poll or at ``finish`` (one last poll after a 3 s grace:
tunnels close when the containers stop and are logged then). Every job's summary is written to
``<logs>/runs/<run_id>.egress.json`` (0640) and logged at INFO ``egress_usage``. The same thread calls
``retry`` (the pending hold requests) every 30 s.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from ossbot.common.config import EgressAnomaly
from ossbot.common.egress_log import JobWindow, Usage, parse_line

log = logging.getLogger(__name__)
POLL_S = 10.0
RETRY_S = 30.0
MAX_READ = 8 * 1024 * 1024
GRACE_S = 3.0
MIB = 1024 * 1024


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


def breached(usage: Usage, limits: EgressAnomaly) -> list[str]:
    out = []
    if len(usage.hosts) > limits.max_hosts:
        out.append("hosts")
    if usage.up > limits.max_upload_mb * MIB:
        out.append("upload")
    if usage.down > limits.max_download_mb * MIB:
        out.append("download")
    return out


def summary(job: Job, end_ts: float | None = None) -> dict[str, Any]:
    return {"run_id": job.run_id, "cid": job.cid, "stage": job.stage, "egress": job.egress,
            "egress_mode": job.egress_mode, "subnet": job.subnet,
            "window": {"start": job.window.start_ts, "end": end_ts}, **job.usage.as_dict(),
            "limits": job.limits.model_dump(), "breached": breached(job.usage, job.limits)}  # fmt: skip


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
        self.malformed = 0
        self._jobs: dict[str, Job] = {}
        self._fh: IO[bytes] | None = None
        self._tail = b""
        self._first = True
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ jobs
    def register(self, run_id: str, cid: str, stage: str, subnet: str, start_ts: float, egress: str,
                 egress_mode: str) -> None:  # fmt: skip
        try:
            limits = self.limits()
        except Exception:  # noqa: BLE001 - an unreadable snapshot must not stop the job: schema defaults
            log.warning("egress limits unreadable; using the defaults", extra={"event": "egress_limits_default"})
            limits = EgressAnomaly()
        with self._lock:
            self._jobs[run_id] = Job(run_id, cid, stage, subnet, egress, egress_mode, limits,
                                     JobWindow.of(subnet, start_ts))  # fmt: skip

    def finish(self, run_id: str) -> dict[str, Any] | None:
        """After the job's containers stopped: grace, last poll, final breach check, summary file."""
        if run_id not in self._jobs:
            return None
        self.sleep(self.grace_s)
        self.poll()
        with self._lock:
            job = self._jobs.pop(run_id, None)
        if job is None:
            return None
        doc = summary(job, self.wall())
        if doc["breached"] and not job.flagged:
            job.flagged = True
            self._report(job.run_id, doc)
        self._write(doc)
        log.info("egress usage of %s: %d hosts, %d B up, %d B down, %d denied", run_id, doc["hosts"],
                 doc["up_bytes"], doc["down_bytes"], doc["denied"],
                 extra={"event": "egress_usage", "run_id": run_id, "cid": job.cid})  # fmt: skip
        return doc

    def _report(self, run_id: str, doc: dict[str, Any]) -> None:
        log.warning("egress anomaly in %s: %s (%d hosts, %d B up, %d B down)", run_id, ",".join(doc["breached"]),
                    doc["hosts"], doc["up_bytes"], doc["down_bytes"],
                    extra={"event": "egress_anomaly", "run_id": run_id, "cid": doc["cid"]})  # fmt: skip
        try:
            self.on_breach(run_id, doc)
        except Exception:
            log.exception("egress anomaly handling failed for %s", run_id, extra={"event": "egress_anomaly_failed"})

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

    # ------------------------------------------------------------------ log reading
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

    def _read(self) -> list[bytes]:
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

    def poll(self) -> None:
        reports: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            for raw in self._read():
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
            for job in self._jobs.values():
                if not job.flagged and breached(job.usage, job.limits):
                    job.flagged = True
                    reports.append((job.run_id, summary(job)))
        for run_id, doc in reports:  # outside the lock: the handler kills containers and calls the gatekeeper
            self._report(run_id, doc)

    # ------------------------------------------------------------------ thread
    def prime(self) -> None:
        """Open the log at its current end (sandboxd start, before any job can be registered)."""
        with self._lock:
            if self._fh is None and self._first:
                self._open(at_end=True)
                self._first = False

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

        self._thread = threading.Thread(target=loop, daemon=True, name="egress-meter")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
