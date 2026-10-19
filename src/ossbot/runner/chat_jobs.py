"""PDF text for the assistant (DESIGN §14.12.5, §14.19 item 9): the runner side of ``chat/pdf_jobs.py``.

The gatekeeper cannot reach sandboxd, so it queues PDF turns; this loop (its own thread, so a long
discovery tick never delays an answer) claims one job at a time, writes the PDF into the runner's input
root and runs ``extract_text`` in profile ``extract`` (``--network none``, no key, pseudo-cid
``sys-chat``). sandboxd key-scans ``/io/output`` into the artifacts the gatekeeper reads; only the run id
and the outcome go back over the API. Nothing is claimed while the gate is closed (kill, pause, drain).
"""

from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path

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


def spec(run_id: str, pdf_path: str) -> dict[str, object]:
    return {"run_id": run_id, "cid": CHAT_CID, "profile": "extract", "entrypoint": "extract_text", "args": {},
            "repo_mode": "none", "inputs": {"doc.pdf": pdf_path}, "timeout_s": TIMEOUT_S, "cpus": 1.0,
            "memory_mb": 512}  # fmt: skip


def run_one(sc: StageContext) -> bool:
    """One claimed job end to end. Returns False when there was nothing to do."""
    if not sc.gate.open(STAGE):
        return False
    claimed = sc.gk.claim_chat_job()
    if claimed is None:
        return False
    job_id, data = claimed
    run_id = new_run_id(STAGE)
    inputs = write_inputs(sc, CHAT_CID, STAGE, {"doc.pdf": data})
    ok = False
    error: str | None = "extract_failed"
    try:
        done = sandbox_run(sc.sbx, spec(run_id, inputs["doc.pdf"]), lambda: sc.gate.check_running(STAGE))
        ok = done.get("state") == "succeeded"
        error = None if ok else str(done.get("error_class") or "extract_failed")[:120]
    except (SbxError, Interrupted, Deferred) as exc:
        error = f"sandbox: {type(exc).__name__}"
        log.warning("extract_text job failed: %s", exc, extra={"event": "chat_extract_failed"})
    finally:
        shutil.rmtree(Path(inputs["doc.pdf"]).parent, ignore_errors=True)
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
