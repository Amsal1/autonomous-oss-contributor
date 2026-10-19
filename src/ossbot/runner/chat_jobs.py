"""The assistant's sandbox jobs, the runner side of ``gatekeeper/chat/{pdf_jobs,code_jobs}.py``.

The gatekeeper cannot reach sandboxd, so it queues them; this loop (its own thread, so a long discovery
tick never delays an answer) claims one job at a time, writes its inputs into the runner's input root and
runs it with the pseudo-cid ``sys-chat``:

* ``pdf``: ``extract_text`` in profile ``extract`` (``--network none``, no key; DESIGN §14.12.5);
* ``code``: ``run_code`` in profile ``code`` (``--network none``, no key, no tokens, exec ``/tmp``
  scratch; DESIGN §15.6, owner decision F007-R1-2): Hermes' program plus the owner's attachments.

sandboxd key-scans ``/io/output`` into the artifacts the gatekeeper reads; only the run id and the outcome
go back over the API. Nothing is claimed while the gate is closed (kill, pause, drain), and a kill or
pause during a run stops it (``check_running``; sandboxd kills the container).
"""

from __future__ import annotations

import base64
import logging
import re
import shutil
import threading
from pathlib import Path
from typing import Any

from ossbot.runner.errors import Deferred, Interrupted, sandbox_run
from ossbot.runner.gk_client import GkError
from ossbot.runner.llm import new_run_id
from ossbot.runner.pipeline import StageContext
from ossbot.runner.sbx_client import SbxError
from ossbot.runner.stages.common import write_inputs

log = logging.getLogger(__name__)
STAGE = "extract"
CHAT_CID = "sys-chat"
POLL_S = 2.0
TIMEOUT_S = 120
CODE_OVERHEAD_S = 30
INPUT_NAME_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")
SOURCES = {"python": "main.py", "bash": "main.sh"}


def spec(run_id: str, pdf_path: str) -> dict[str, object]:
    return {"run_id": run_id, "cid": CHAT_CID, "profile": "extract", "entrypoint": "extract_text", "args": {},
            "repo_mode": "none", "inputs": {"doc.pdf": pdf_path}, "timeout_s": TIMEOUT_S, "cpus": 1.0,
            "memory_mb": 512}  # fmt: skip


def code_spec(run_id: str, language: str, timeout_s: int, inputs: dict[str, str]) -> dict[str, object]:
    return {"run_id": run_id, "cid": CHAT_CID, "profile": "code", "entrypoint": "run_code",
            "args": {"language": language, "timeout_s": timeout_s}, "repo_mode": "none", "inputs": inputs,
            "timeout_s": timeout_s + CODE_OVERHEAD_S, "cpus": 1.0, "memory_mb": 1024}  # fmt: skip


def code_inputs(job: dict[str, Any]) -> tuple[str, int, dict[str, str | bytes]]:
    """``(language, timeout_s, files)`` of a claimed code job; the gatekeeper's names are re-checked."""
    language = str(job.get("language"))
    if language not in SOURCES:
        raise ValueError("language")
    timeout_s = int(job.get("timeout_s") or 0)
    if not 1 <= timeout_s <= 120:
        raise ValueError("timeout_s")
    files: dict[str, str | bytes] = {SOURCES[language]: base64.b64decode(str(job["code_b64"]), validate=True)}
    for f in job.get("files") or []:
        name = str(f.get("name"))
        if not INPUT_NAME_RE.match(name) or name in files or name in SOURCES.values() or len(files) > 5:
            raise ValueError("file name")
        files[name] = base64.b64decode(str(f.get("b64")), validate=True)
    return language, timeout_s, files


def _run(sc: StageContext, kind: str, job: dict[str, Any]) -> tuple[bool, str | None, str]:
    """Run one claimed job in its sandbox: ``(ok, error, run_id)``."""
    run_id = new_run_id(STAGE if kind == "pdf" else "code")
    language, timeout_s = "", 0
    files: dict[str, str | bytes]
    if kind == "pdf":
        files = {"doc.pdf": base64.b64decode(str(job["pdf_b64"]), validate=True)}
    elif kind == "code":
        language, timeout_s, files = code_inputs(job)
    else:
        raise ValueError(f"kind {kind[:20]}")
    inputs = write_inputs(sc, CHAT_CID, STAGE if kind == "pdf" else "code", files)
    try:
        job_spec = spec(run_id, inputs["doc.pdf"]) if kind == "pdf" else code_spec(run_id, language, timeout_s, inputs)
        done = sandbox_run(sc.sbx, job_spec, lambda: sc.gate.check_running(STAGE))
        ok = done.get("state") == "succeeded"
        return ok, None if ok else str(done.get("error_class") or f"{kind}_failed")[:120], run_id
    except (SbxError, Interrupted, Deferred) as exc:
        log.warning("%s job failed: %s", kind, exc, extra={"event": "chat_job_failed", "kind": kind})
        return False, f"sandbox: {type(exc).__name__}", run_id
    finally:
        shutil.rmtree(Path(next(iter(inputs.values()))).parent, ignore_errors=True)


def run_one(sc: StageContext) -> bool:
    """One claimed job end to end. Returns False when there was nothing to do."""
    if not sc.gate.open(STAGE):
        return False
    job = sc.gk.claim_chat_job()
    if job is None:
        return False
    job_id, kind = str(job["job_id"]), str(job.get("kind") or "pdf")
    try:
        ok, error, run_id = _run(sc, kind, job)
    except (ValueError, KeyError) as exc:  # a malformed claim: reported, never run
        ok, error, run_id = False, f"bad_job: {exc}"[:120], ""
    sc.gk.chat_job_result(job_id, run_id=run_id if ok else None, ok=ok, error=error)
    return True


def loop(sc: StageContext, stop: threading.Event, poll_s: float = POLL_S) -> None:
    while not stop.is_set():
        try:
            busy = run_one(sc)
        except (GkError, OSError, ValueError) as exc:
            log.warning("chat job poll failed: %s", exc, extra={"event": "chat_jobs_poll_failed"})
            busy = False
        if not busy:
            stop.wait(poll_s)


def start(sc: StageContext, stop: threading.Event) -> threading.Thread:
    thread = threading.Thread(target=loop, args=(sc, stop), name="chat-jobs", daemon=True)
    thread.start()
    return thread
