"""``POST /v1/chat/selftest`` (operator; DESIGN §15.6, PLAN items 37/38).

One real turn through ``ChatService`` -> Hermes -> the kiro-acp V3 provider, on ``roles.test`` (or
``test_vision`` with an image, MDL-3). The answer goes to the owner's Telegram chat only; the caller
gets redacted metadata (engine, mode, model, latency, answer length, tools), never the answer. An
optional fixture from ``tests/fixtures/chat/`` is attached the way an owner's file would be.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ossbot.common.models import requested
from ossbot.ctl import hermes_probe
from ossbot.gatekeeper import db
from ossbot.gatekeeper.api.deps import ctx
from ossbot.gatekeeper.authz import Caller, Role, require
from ossbot.gatekeeper.chat import media
from ossbot.gatekeeper.chat.backend import RelayClosed
from ossbot.gatekeeper.chat.code_jobs import CODE_MAX, FILES_IN_MAX, TIMEOUT_MAX_S, CodeRefused
from ossbot.gatekeeper.chat.code_jobs import JOB_ID_RE as CODE_JOB_ID_RE
from ossbot.gatekeeper.chat.pdf_jobs import JOB_ID_RE

ATTACHMENT_ID_PATTERN = r"^a_[0-9a-f]{16}$"

router = APIRouter(prefix="/v1")
DEFAULT_PROMPT = "This is an ossbot self-test. Reply with one short sentence confirming you can read this."
FIXTURE_RE = r"^[a-z0-9][a-z0-9_-]{0,63}\.(jpg|jpeg|png|webp|txt|md|csv|json|pdf)$"
NOTIFY_MAX = 3800
_busy = asyncio.Lock()


class SelftestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    prompt: Annotated[str, Field(min_length=1, max_length=2000)] = DEFAULT_PROMPT
    fixture: Annotated[str, Field(pattern=FIXTURE_RE)] | None = None
    deliver: bool = True


def _read_fixture(root: Any, name: str, max_bytes: int) -> bytes:
    fd = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise media.AttachmentRefused("too_large", "fixture larger than the attachment limit")
    return data


@router.post("/chat/selftest")
async def selftest(
    request: Request, body: SelftestRequest, caller: Annotated[Caller, Depends(require(Role.OPERATOR))]
) -> Any:
    c = ctx(request)
    if c.chat is None:
        return JSONResponse(status_code=503, content={"error": "chat_unconfigured"})
    if _busy.locked():
        return JSONResponse(status_code=409, content={"error": "selftest_running"})
    async with _busy:
        policy = c.config.policy.chat.attachments
        parts: list[dict[str, Any]] = [{"type": "input_text", "text": body.prompt}]
        attachment: dict[str, Any] | None = None
        if body.fixture is not None:
            try:
                data = _read_fixture(c.paths.repo / "tests" / "fixtures" / "chat", body.fixture,
                                     policy.max_mb * 1024 * 1024)  # fmt: skip
                kind = media.classify(None, body.fixture)
                prepared = await c.chat.prepare(kind, data, body.fixture, policy)
            except FileNotFoundError:
                return JSONResponse(status_code=404, content={"error": "fixture_not_found"})
            except media.AttachmentRefused as exc:
                return JSONResponse(status_code=400, content={"error": "attachment_refused", "reason": exc.reason,
                                                              "reply": exc.reply})  # fmt: skip
            attachment_id = c.chat.media.put("selftest", body.fixture, prepared.data)
            note = (f"The attached file {body.fixture} has attachment id {attachment_id}; ossbot_request_sandbox_job"
                    " can read it from /io/input.")  # fmt: skip
            parts += [*prepared.parts, {"type": "input_text", "text": note}]
            attachment = {"kind": prepared.kind, "bytes": len(prepared.data), "metadata_removed": prepared.removed,
                          "id": attachment_id}  # fmt: skip
        role = "test_vision" if attachment and attachment["kind"] == "image" else "test"
        try:
            model = requested(c.models.read(), role)[0]
        except (OSError, ValueError):
            model = None
        session_key = f"selftest-{int(time.time())}"
        try:
            outcome = await c.chat.service.ask(
                parts,
                session_key,
                origin="media" if attachment else "typed",
                had_attachments=attachment is not None,
                request_role="test",
                model_label=model,
            )
        except RelayClosed as exc:
            closed = {"error": "relay_closed", "reason": exc.gate.reason, "detail": exc.gate.detail,
                      "reply": exc.gate.reply}  # fmt: skip
            return JSONResponse(status_code=503, content=closed)
        delivered = False
        if body.deliver and outcome.status == "done" and outcome.answer:
            text = outcome.answer if len(outcome.answer) <= NOTIFY_MAX else outcome.answer[:NOTIFY_MAX] + "…"
            c.notify(f"🤖 Assistant · selftest\n\n{text}")
            delivered = True
        probe = hermes_probe.read_result(c.chat.probe_path) or {}
        key_doc = probe.get("key")
        key: dict[str, Any] = key_doc if isinstance(key_doc, dict) else {}
        result = {
            "status": outcome.status,
            "error": outcome.error or None,
            "engine": "kiro-acp v3",
            "kiro_cli": key.get("kiro_cli_version"),
            "mode": probe.get("mode"),
            "role": role,
            "model": model,
            "latency_s": round(outcome.latency_s, 2),
            "answer_chars": len(outcome.answer),
            "streamed_deltas": outcome.streamed_deltas,
            "tools": outcome.tools,
            "attachment": attachment,
            "turn_id": outcome.turn_id,
            "delivered": delivered,
        }
        with db.transaction(c.conn):
            c.audit.append(caller.actor, "chat_selftest", {k: result[k] for k in ("status", "role", "model",
                           "answer_chars", "tools", "delivered")} | {"fixture": body.fixture})  # fmt: skip
        return result


# ------------------------------------------------------------------ PDF text jobs (runner)
class JobResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,30}$")] | None = None
    ok: bool
    error: Annotated[str, Field(max_length=120)] | None = None


@router.post("/chat/jobs/claim")
async def claim_job(request: Request, caller: Annotated[Caller, Depends(require(Role.WORKER))]) -> dict[str, Any]:
    """Runner: the next ``extract_text`` (``kind: pdf``) or ``run_code`` (``kind: code``) job; ``job`` is
    null when there is none. PDF turns wait for an answer, so they go first."""
    c = ctx(request)
    if c.chat is None:
        return {"job": None}
    job = c.chat.pdf.claim() if c.chat.pdf is not None else None
    if job is not None:
        return {"job": {**job, "kind": "pdf"}}
    return {"job": c.chat.code.claim() if c.chat.code is not None else None}


@router.post("/chat/jobs/{job_id}/result")
async def job_result(request: Request, job_id: str, body: JobResult,
                     caller: Annotated[Caller, Depends(require(Role.WORKER))]) -> Any:  # fmt: skip
    c = ctx(request)
    if c.chat is None:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    if JOB_ID_RE.match(job_id) and c.chat.pdf is not None:
        max_bytes = c.config.policy.chat.attachments.pdf_text_max_bytes
        found, event = c.chat.pdf.finish(job_id, run_id=body.run_id, ok=body.ok, error=body.error,
                                         max_bytes=max_bytes), "chat_pdf_job"  # fmt: skip
    elif CODE_JOB_ID_RE.match(job_id) and c.chat.code is not None:
        found, event = c.chat.code.finish(job_id, run_id=body.run_id, ok=body.ok, error=body.error), "chat_code_job"
    else:
        found, event = False, ""
    if not found:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    with db.transaction(c.conn):
        c.audit.append(caller.actor, event, {"job_id": job_id, "run_id": body.run_id, "ok": body.ok,
                                             "error": body.error})  # fmt: skip
    return {"accepted": True}


# ------------------------------------------------------------------ code jobs (Hermes, §15.6)
class CodeJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["code"]
    language: Literal["python", "bash"]
    code: Annotated[str, Field(min_length=1, max_length=CODE_MAX)]
    files: Annotated[list[Annotated[str, Field(pattern=ATTACHMENT_ID_PATTERN)]], Field(max_length=FILES_IN_MAX)] = []
    timeout_s: Annotated[int, Field(ge=1, le=TIMEOUT_MAX_S)] = TIMEOUT_MAX_S


@router.post("/chat/sandbox-jobs", status_code=202)
async def submit_code_job(request: Request, body: CodeJobRequest,
                          caller: Annotated[Caller, Depends(require(Role.REQUESTER))]) -> Any:  # fmt: skip
    """Hermes: run a short program in a ``--network none`` sandbox (tier ``self_improve`` only)."""
    c = ctx(request)
    if c.chat is None or c.chat.code is None:
        return JSONResponse(status_code=503, content={"error": "chat_unconfigured"})
    tier = c.chat.tier()
    if tier != "self_improve":
        return JSONResponse(status_code=403, content={"error": "tier", "detail": f"code jobs need tier self_improve"
                                                                                 f" (active: {tier})"})  # fmt: skip
    try:
        job = c.chat.code.submit(body.language, body.code, body.files, body.timeout_s)
    except CodeRefused as exc:
        return JSONResponse(status_code=exc.status, content={"error": exc.reason})
    digest = hashlib.sha256(body.code.encode()).hexdigest()
    with db.transaction(c.conn):
        c.audit.append(caller.actor, "chat_code_job_submitted", {"job_id": job.id, "language": body.language,
                       "code_sha256": digest, "files": len(body.files), "timeout_s": body.timeout_s})  # fmt: skip
    return c.chat.code.view(job)


@router.get("/chat/sandbox-jobs/{job_id}")
async def get_code_job(request: Request, job_id: str,
                       caller: Annotated[Caller, Depends(require(Role.REQUESTER))]) -> Any:  # fmt: skip
    c = ctx(request)
    job = None if c.chat is None or c.chat.code is None else c.chat.code.get(job_id)
    if job is None or c.chat is None or c.chat.code is None:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    return c.chat.code.view(job)
