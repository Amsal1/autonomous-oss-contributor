"""PDF text for the assistant via the runner's ``extract_text`` sandbox job (DESIGN §14.12.5, §14.19 item 9).

The gatekeeper never parses a PDF and cannot reach sandboxd (only root and ``ossrunner`` can), so a PDF
turn queues a job here and waits; the runner claims it (``POST /v1/chat/jobs/claim``, worker role), runs
``extract_text`` in profile ``extract`` (``--network none``, ``pdftotext`` from ``sandbox-full``, pseudo-cid
``sys-chat``) and reports the run (``POST /v1/chat/jobs/{id}/result``). The text is read from the job's
key-scanned artifacts (``<artifacts>/sys-chat/runs/<run_id>/text.txt``, group ``ossart``), capped at
``pdf_text_max_bytes``. Jobs live in memory only: a waiting turn does not survive a gatekeeper restart.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

CHAT_CID = "sys-chat"
JOB_ID_RE = re.compile(r"^x_[0-9a-f]{16}$")
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
WAIT_S = 240.0
CLAIM_STALE_S = 600.0
MAX_QUEUED = 4


class PdfUnavailable(RuntimeError):
    """No text: the queue is full, the runner did not answer in time, or the job failed."""


@dataclass
class Job:
    id: str
    data: bytes
    created: float
    status: Literal["queued", "claimed", "done", "failed"] = "queued"
    claimed: float | None = None
    run_id: str | None = None
    error: str | None = None
    text: bytes = b""
    done: asyncio.Event = field(default_factory=asyncio.Event)


class PdfJobs:
    def __init__(self, artifacts_dir: Path, *, now: Callable[[], float] = time.monotonic,
                 wait_s: float = WAIT_S) -> None:  # fmt: skip
        self.artifacts_dir, self._now, self.wait_s = artifacts_dir, now, wait_s
        self._jobs: OrderedDict[str, Job] = OrderedDict()

    def pending(self) -> int:
        return sum(1 for j in self._jobs.values() if j.status in ("queued", "claimed"))

    async def extract(self, data: bytes, max_bytes: int) -> bytes:
        """Queue one PDF and wait for its text (raises ``PdfUnavailable``)."""
        if self.pending() >= MAX_QUEUED:
            raise PdfUnavailable("queue_full")
        job = Job(f"x_{secrets.token_hex(8)}", data, self._now())
        self._jobs[job.id] = job
        try:
            await asyncio.wait_for(job.done.wait(), self.wait_s)
        except TimeoutError as exc:
            raise PdfUnavailable("timeout") from exc
        finally:
            self._jobs.pop(job.id, None)
        if job.status != "done":
            raise PdfUnavailable(job.error or "failed")
        return job.text[: max_bytes + 1]

    def claim(self) -> dict[str, Any] | None:
        """Runner: the oldest queued job (and claimed ones whose runner went away)."""
        now = self._now()
        for job in self._jobs.values():
            stale = job.status == "claimed" and job.claimed is not None and now - job.claimed > CLAIM_STALE_S
            if job.status == "queued" or stale:
                job.status, job.claimed = "claimed", now
                return {"job_id": job.id, "pdf_b64": base64.b64encode(job.data).decode("ascii"),
                        "bytes": len(job.data)}  # fmt: skip
        return None

    def finish(self, job_id: str, *, run_id: str | None, ok: bool, error: str | None, max_bytes: int) -> bool:
        """Runner: the job's outcome. Returns False for an unknown or already finished job."""
        job = self._jobs.get(job_id)
        if job is None or job.status != "claimed":
            return False
        if ok and run_id is not None and RUN_ID_RE.match(run_id):
            try:
                job.text, job.status = self._read_text(run_id, max_bytes), "done"
            except OSError as exc:
                job.status, job.error = "failed", f"text_unreadable: {type(exc).__name__}"
        else:
            job.status, job.error = "failed", (error or "extract_failed")[:120]
        job.run_id = run_id
        job.done.set()
        return True

    def _read_text(self, run_id: str, max_bytes: int) -> bytes:
        path = self.artifacts_dir / CHAT_CID / "runs" / run_id / "text.txt"
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as fh:
            return fh.read(max_bytes + 1)
