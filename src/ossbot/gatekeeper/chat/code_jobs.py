"""The assistant's code jobs (DESIGN §15.6; owner decision 2026-10-19 on F007-R1-2).

Hermes asks through the ``ossbot`` plugin (``ossbot_request_sandbox_job``, kind ``code``) to run a short
python or bash program on the owner's attachments. The gatekeeper queues it here; the runner claims it
(``POST /v1/chat/jobs/claim``, worker role) and runs ``run_code`` in a fresh ``sandbox-full`` container
of profile ``code`` (``--network none``, no key, no tokens, read-only rootfs, exec ``/tmp`` scratch,
120 s), then reports the run. The result is read from the job's key-scanned artifacts with the caps
enforced again here (the code shares the entrypoint's uid), text is redacted, result files with a
secret hit are dropped, and the kept files become attachments with their own ids.

Limits: one running and two queued jobs, 20 per hour. Only the ``self_improve`` tier may submit, and
nothing is accepted or handed out while ossbot is killed, paused or the workers are down (queued jobs
are cancelled then). Jobs live in memory only: a gatekeeper restart forgets them.
"""

from __future__ import annotations

import base64
import collections
import json
import os
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ossbot.gatekeeper.chat.media import MediaStore, safe_name

CHAT_CID = "sys-chat"
JOB_ID_RE = re.compile(r"^k_[0-9a-f]{16}$")
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
MAX_RUNNING = 1
MAX_QUEUED = 2
PER_HOUR = 20
CODE_MAX = 64 * 1024
TIMEOUT_MAX_S = 120
FILES_IN_MAX = 5
INPUTS_MAX = 40 * 1024 * 1024
STREAM_MAX = 32 * 1024
FILES_OUT_MAX = 5
FILES_OUT_TOTAL = 10 * 1024 * 1024
OUT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
OUT_EXT_RE = re.compile(r"\.(png|jpg|jpeg|webp|gif|svg|csv|tsv|txt|md|json|html|pdf)$", re.IGNORECASE)
CLAIM_GRACE_S = 120.0
KEEP_FINISHED = 20
OUTPUT_SESSION = "sandbox-output"
Language = Literal["python", "bash"]
Status = Literal["queued", "running", "done", "failed", "cancelled"]
SOURCES: dict[str, str] = {"python": "main.py", "bash": "main.sh"}


class CodeRefused(ValueError):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status, self.reason = status, reason


@dataclass
class CodeJob:
    id: str
    language: Language
    code: str
    files: list[tuple[str, bytes]]
    timeout_s: int
    created: float
    status: Status = "queued"
    claimed: float | None = None
    finished: float | None = None
    run_id: str | None = None
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)


def input_name(attachment_id: str, name: str) -> str:
    """A stage-in input name (``^[a-z0-9_.-]{1,64}$``) for an attachment: ``<id>-<safe name>``, so the
    path is known before the code is written, whatever the order of ``files``."""
    base = safe_name(name, "file").lower().replace("..", ".")
    return f"{attachment_id}-{base}"[:64]


def input_path(attachment_id: str, name: str) -> str:
    """Where ``run_code`` finds an attachment passed in ``files`` (the note Hermes gets with it)."""
    return f"/io/input/{input_name(attachment_id, name)}"


def attachment_note(attachment_id: str, name: str) -> str:
    """The text part that goes with a kept attachment, so Hermes can hand it to a code job."""
    return (f"The attached file {safe_name(name, 'file')} has attachment id {attachment_id}. To process it with"
            f" ossbot_request_sandbox_job, pass {attachment_id} in files; the program then reads it at"
            f" {input_path(attachment_id, name)}.")  # fmt: skip


class CodeJobs:
    def __init__(
        self,
        artifacts_dir: Path,
        media: MediaStore,
        *,
        redact: Callable[[str], str],
        stopped: Callable[[], str | None],
        now: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.artifacts_dir, self.media, self._redact, self._stopped = artifacts_dir, media, redact, stopped
        self._now, self._wall = now, wall
        self._jobs: OrderedDict[str, CodeJob] = OrderedDict()
        self._submitted: collections.deque[float] = collections.deque()

    # ------------------------------------------------------------------ Hermes side
    def _sweep(self) -> None:
        reason = self._stopped()
        now = self._now()
        for job in self._jobs.values():
            if job.status == "queued" and reason is not None:
                job.status, job.error, job.finished = "cancelled", f"stopped: {reason}", now
            elif (job.status == "running" and job.claimed is not None
                  and now - job.claimed > job.timeout_s + CLAIM_GRACE_S):  # fmt: skip
                job.status, job.error, job.finished = "failed", "runner_lost", now
        done = [j.id for j in self._jobs.values() if j.status in ("done", "failed", "cancelled")]
        for job_id in done[: max(0, len(done) - KEEP_FINISHED)]:
            del self._jobs[job_id]

    def submit(self, language: Language, code: str, attachment_ids: list[str], timeout_s: int) -> CodeJob:
        self._sweep()
        reason = self._stopped()
        if reason is not None:
            raise CodeRefused(423, f"stopped: {reason}")
        if not code.strip() or len(code.encode("utf-8")) > CODE_MAX:
            raise CodeRefused(400, "code_size")
        if not 1 <= timeout_s <= TIMEOUT_MAX_S:
            raise CodeRefused(400, "timeout")
        if len(attachment_ids) > FILES_IN_MAX or len(set(attachment_ids)) != len(attachment_ids):
            raise CodeRefused(400, "files")
        wall = self._wall()
        while self._submitted and wall - self._submitted[0] > 3600:
            self._submitted.popleft()
        if len(self._submitted) >= PER_HOUR:
            raise CodeRefused(429, "rate_limited")
        if sum(1 for j in self._jobs.values() if j.status == "queued") >= MAX_QUEUED:
            raise CodeRefused(429, "queue_full")
        files: list[tuple[str, bytes]] = []
        for attachment_id in attachment_ids:
            got = self.media.get(attachment_id)
            if got is None:
                raise CodeRefused(404, f"attachment_not_found: {attachment_id[:20]}")
            files.append((input_name(attachment_id, got[0]), got[1]))
        if sum(len(d) for _, d in files) > INPUTS_MAX:
            raise CodeRefused(413, "inputs_too_large")
        job = CodeJob(f"k_{secrets.token_hex(8)}", language, code, files, timeout_s, self._now())
        self._jobs[job.id] = job
        self._submitted.append(wall)
        return job

    def get(self, job_id: str) -> CodeJob | None:
        self._sweep()
        return self._jobs.get(job_id) if JOB_ID_RE.match(job_id) else None

    def view(self, job: CodeJob) -> dict[str, Any]:
        out: dict[str, Any] = {"job_id": job.id, "status": job.status, "language": job.language}
        if job.error:
            out["error"] = job.error
        return out | job.result

    def pending(self) -> int:
        return sum(1 for j in self._jobs.values() if j.status in ("queued", "running"))

    # ------------------------------------------------------------------ runner side
    def claim(self) -> dict[str, Any] | None:
        """The oldest queued job, only while none runs (one running job)."""
        self._sweep()
        if self._stopped() is not None or sum(1 for j in self._jobs.values() if j.status == "running") >= MAX_RUNNING:
            return None
        for job in self._jobs.values():
            if job.status == "queued":
                job.status, job.claimed = "running", self._now()
                return {
                    "job_id": job.id, "kind": "code", "language": job.language, "timeout_s": job.timeout_s,
                    "source": SOURCES[job.language], "code_b64": base64.b64encode(job.code.encode()).decode(),
                    "files": [{"name": n, "b64": base64.b64encode(d).decode()} for n, d in job.files],
                }  # fmt: skip
        return None

    def finish(self, job_id: str, *, run_id: str | None, ok: bool, error: str | None) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status != "running":
            return False
        job.run_id, job.finished = run_id, self._now()
        if ok and run_id is not None and RUN_ID_RE.match(run_id):
            try:
                job.result = self._read(self.artifacts_dir / CHAT_CID / "runs" / run_id)
                job.status = "done"
            except (OSError, ValueError) as exc:
                job.status, job.error = "failed", f"result_unreadable: {type(exc).__name__}"
        else:
            job.status, job.error = "failed", (error or "run_failed")[:120]
        job.files = []  # inputs are not kept once the job ran
        return True

    # ------------------------------------------------------------------ artifacts
    def _read_capped(self, path: Path, limit: int) -> tuple[bytes, bool]:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as fh:
            data = fh.read(limit + 1)
        return data[:limit], len(data) > limit

    def _text(self, run: Path, name: str, reported: object) -> tuple[str, bool]:
        try:
            data, over = self._read_capped(run / name, STREAM_MAX)
        except FileNotFoundError:
            return "", False
        truncated = over or (isinstance(reported, int) and reported > STREAM_MAX)
        return self._redact(data.decode("utf-8", "replace")), truncated

    def _read(self, run: Path) -> dict[str, Any]:
        raw, over = self._read_capped(run / "result.json", 64 * 1024)
        doc = json.loads(raw) if not over else None
        if not isinstance(doc, dict):
            raise ValueError("result.json")
        if doc.get("ok") is not True:
            return {"exit_code": None, "error": str(doc.get("error_class") or "run_failed")[:80]}
        stdout, out_trunc = self._text(run, "stdout.txt", doc.get("stdout_bytes"))
        stderr, err_trunc = self._text(run, "stderr.txt", doc.get("stderr_bytes"))
        files: list[dict[str, Any]] = []
        dropped = [str(n)[:64] for n in doc.get("dropped") or [] if isinstance(n, str)][:20]
        total = 0
        for entry in sorted(run.glob("out-*")):
            name = entry.name[4:]
            if entry.is_symlink() or not entry.is_file() or not OUT_NAME_RE.match(name) or not OUT_EXT_RE.search(name):
                dropped.append(name[:64])
                continue
            if entry.stat().st_size > FILES_OUT_TOTAL:
                dropped.append(name)
                continue
            data, _ = self._read_capped(entry, FILES_OUT_TOTAL)
            if self._redact(data.decode("latin-1")) != data.decode("latin-1"):
                dropped.append(f"{name} (secret)")
                continue
            if len(files) >= FILES_OUT_MAX or total + len(data) > FILES_OUT_TOTAL:
                dropped.append(name)
                continue
            total += len(data)
            files.append({"id": self.media.put(OUTPUT_SESSION, name, data), "name": name, "bytes": len(data)})
        code = doc.get("exit_code")
        return {
            "exit_code": code if isinstance(code, int) else None, "timed_out": doc.get("timed_out") is True,
            "stdout": stdout, "stdout_truncated": out_trunc, "stderr": stderr, "stderr_truncated": err_trunc,
            "files": files, "dropped": dropped,
        }  # fmt: skip
