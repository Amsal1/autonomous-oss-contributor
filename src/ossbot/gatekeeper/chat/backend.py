"""Assistant backend: the relay gate and the Hermes API backend (DESIGN §15.2 M9, §15.6, §14.12).

Every turn passes ``RelayGate.check`` first. The relay is open only when:

1. ossbot is neither killed nor paused, and the workers are not draining or down;
2. the root Hermes-side Kiro probe passed less than 2 h ago (``/var/lib/ossbot/probes/hermes-kiro.json``,
   ``ossbot.common.probe_result.relay_open``);
3. Hermes' tool surface (``GET /v1/toolsets``) is within the tier's allowlist. Drift or an unverifiable
   answer soft-kills ``hermes_tool_surface``; refused or timed out only closes the relay. An ``ok`` is
   reused for ``SURFACE_TTL_S``.

``HermesApiBackend.stream`` yields the ``hermes_api`` events of one turn (``/v1/responses`` with
``stream: true`` and the session key). Cancelling the consuming task closes the HTTP stream, and Hermes
interrupts the run. ``ChatService.ask`` runs one whole turn on top (a ``chat_turns`` row, the redacted
answer); the selftest uses it, the streaming UI (PLAN item 49) consumes ``stream`` directly. Image turns
run on ``roles.vision``: the provider picks the role from the input parts.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from ossbot.common import probe_result
from ossbot.common.config import AttachmentKind, AttachmentsPolicy
from ossbot.common.control_flags import FlagState, HermesTier
from ossbot.gatekeeper.chat import hermes_api, media
from ossbot.gatekeeper.chat.code_jobs import CodeJobs
from ossbot.gatekeeper.chat.hermes_api import Event, HermesClient, HermesError, HermesUnavailable
from ossbot.gatekeeper.chat.hermes_surface import SurfaceResult
from ossbot.gatekeeper.chat.media import MediaStore, Prepared
from ossbot.gatekeeper.chat.pdf_jobs import PdfJobs, PdfUnavailable
from ossbot.gatekeeper.chat.turns import Origin, TurnStore
from ossbot.gatekeeper.telegram.ui.redact import redact

SURFACE_TTL_S = 60.0
GateReason = Literal["ok", "killed", "paused", "workers_down", "probe_pending", "hermes_unavailable", "surface"]
REPLIES: dict[str, str] = {
    "killed": "🤖 Assistant is stopped while ossbot is killed.",
    "paused": "🤖 Assistant is stopped while ossbot is paused.",
    "workers_down": "🤖 Assistant is stopped (workers down) — /up",
    "probe_pending": "🤖 Assistant unavailable (safety probe pending).",
    "hermes_unavailable": "🤖 Assistant unavailable — Hermes not responding. OSS commands still work.",
    "surface": "🤖 Assistant disabled: the Hermes tool surface check failed.",
}


@dataclass(frozen=True)
class GateResult:
    open: bool
    reason: GateReason
    detail: str = ""

    @property
    def reply(self) -> str:
        return REPLIES.get(self.reason, "")


class RelayClosed(RuntimeError):
    def __init__(self, gate: GateResult) -> None:
        super().__init__(gate.reason)
        self.gate = gate


class RelayGate:
    """The M9 gate. ``surface`` makes a blocking HTTP call and runs in a worker thread."""

    def __init__(
        self,
        *,
        flags: Callable[[], FlagState],
        lifecycle: Callable[[], str | None],
        probe_doc: Callable[[], Mapping[str, Any] | None],
        surface: Callable[[HermesTier], SurfaceResult],
        tier: Callable[[], HermesTier],
        on_surface_kill: Callable[[SurfaceResult], None],
        now: Callable[[], float] = time.time,
    ) -> None:
        self._flags, self._lifecycle, self._probe_doc = flags, lifecycle, probe_doc
        self._surface, self._tier, self._on_kill, self._now = surface, tier, on_surface_kill, now
        self._surface_ok: tuple[float, HermesTier] | None = None

    def _local(self) -> GateResult | None:
        flags = self._flags()
        if flags.killed is not None:
            return GateResult(False, "killed")
        if flags.paused is not None:
            return GateResult(False, "paused")
        if self._lifecycle() is not None:
            return GateResult(False, "workers_down")
        if not probe_result.relay_open(self._probe_doc(), now=self._now()):
            return GateResult(False, "probe_pending")
        return None

    async def check(self) -> GateResult:
        closed = self._local()
        if closed is not None:
            return closed
        tier = self._tier()
        cached = self._surface_ok
        if cached is not None and cached[1] == tier and self._now() - cached[0] < SURFACE_TTL_S:
            return GateResult(True, "ok")
        result = await asyncio.to_thread(self._surface, tier)
        if result.outcome == "ok":
            self._surface_ok = (self._now(), tier)
            return GateResult(True, "ok")
        self._surface_ok = None
        if result.kill:
            self._on_kill(result)
            return GateResult(False, "surface", f"{result.outcome}: {result.detail}")
        return GateResult(False, "hermes_unavailable", result.detail)

    def invalidate(self) -> None:
        self._surface_ok = None


class ChatBackend(Protocol):
    async def gate(self) -> GateResult: ...

    def stream(self, parts: list[dict[str, Any]], session_key: str, *, request_role: str | None = None
               ) -> AsyncIterator[Event]: ...  # fmt: skip


class HermesApiBackend:
    """``ChatBackend`` over the Hermes API server (127.0.0.1:8642)."""

    PROVIDER = "kiro-acp"

    def __init__(self, client: HermesClient, gate: RelayGate) -> None:
        self.client, self._gate = client, gate

    async def gate(self) -> GateResult:
        return await self._gate.check()

    async def stream(self, parts: list[dict[str, Any]], session_key: str, *, request_role: str | None = None
                     ) -> AsyncIterator[Event]:  # fmt: skip
        """Gate, then the turn's events. ``request_role`` (e.g. ``test`` for live checks, MDL-3) is sent
        as ``model="role:<name>"``; the provider honours only its allowlisted request roles."""
        gate = await self._gate.check()
        if not gate.open:
            raise RelayClosed(gate)
        model = f"role:{request_role}" if request_role else None
        try:
            async for event in self.client.stream(parts, session_key, model=model,
                                                  provider=self.PROVIDER if model else None):  # fmt: skip
                yield event
        except HermesUnavailable:
            self._gate.invalidate()
            raise


@dataclass
class TurnOutcome:
    turn_id: int
    status: Literal["done", "failed"]
    answer: str = ""  # redacted
    tools: list[str] = field(default_factory=list)
    latency_s: float = 0.0
    response_id: str = ""
    error: str = ""
    streamed_deltas: int = 0


class ChatService:
    """One whole turn: ``chat_turns`` row, the backend stream, the redacted answer."""

    def __init__(self, backend: ChatBackend, turns: TurnStore, *, monotonic: Callable[[], float] = time.monotonic
                 ) -> None:  # fmt: skip
        self.backend, self.turns, self._mono = backend, turns, monotonic

    async def ask(self, parts: list[dict[str, Any]], session_key: str, *, user_message_id: int = 0,
                  origin: Origin = "typed", had_attachments: bool = False, request_role: str | None = None,
                  model_label: str | None = None) -> TurnOutcome:  # fmt: skip
        """Raises ``RelayClosed`` before a row is written when the gate is closed. A cancel marks the
        row ``stopped`` (``finalize``) and propagates."""
        gate = await self.backend.gate()
        if not gate.open:
            raise RelayClosed(gate)
        turn_id = self.turns.start(session_key, user_message_id, origin=origin, had_attachments=had_attachments)
        started = self._mono()
        deltas: list[str] = []
        kept = 0  # characters kept from the deltas; the answer is bounded by hermes_api.MAX_ANSWER
        streamed = 0
        tools: list[str] = []
        final: str | None = None
        response_id = ""
        try:
            async for event in self.backend.stream(parts, session_key, request_role=request_role):
                if event.kind == "delta":
                    streamed += 1
                    if kept < hermes_api.MAX_ANSWER:
                        piece = event.text[: hermes_api.MAX_ANSWER - kept]
                        deltas.append(piece)
                        kept += len(piece)
                elif event.kind == "tool" and event.tool:
                    tools.append(event.tool)
                elif event.kind == "completed":
                    final, response_id = hermes_api.final_text(event.data), event.response_id
                elif event.kind == "failed":
                    raise HermesError(event.text)
        except asyncio.CancelledError:
            self.turns.finalize(turn_id, cancelled=True, failed=False)
            raise
        except (HermesError, HermesUnavailable, RelayClosed) as exc:
            self.turns.finalize(turn_id, cancelled=False, failed=True)
            err = redact(str(exc)[:300])[0]
            return TurnOutcome(turn_id, "failed", tools=tools, latency_s=self._mono() - started, error=err)
        answer = final if final else "".join(deltas)
        stored = self.turns.done(turn_id, answer, model=model_label, tools=tools, run_id=response_id or None)
        return TurnOutcome(turn_id, "done", stored, tools, self._mono() - started, response_id,
                           streamed_deltas=streamed)  # fmt: skip


@dataclass
class ChatRuntime:
    """What the gatekeeper holds for the assistant (``AppContext.chat``)."""

    service: ChatService
    gate: RelayGate
    media: MediaStore
    pdf: PdfJobs | None
    probe_path: Path
    code: CodeJobs | None = None
    tier: Callable[[], HermesTier] = field(default=lambda: "locked")

    async def prepare(self, kind: AttachmentKind, data: bytes, name: str, policy: AttachmentsPolicy, *,
                      as_file: bool = False) -> Prepared:  # fmt: skip
        """``media.prepare`` plus the PDF text from the sandbox job (refused while unavailable)."""
        prepared = media.prepare(kind, data, name, policy, as_file=as_file)
        if not prepared.needs_pdf_text:
            return prepared
        if self.pdf is None:
            raise media.AttachmentRefused("pdf_unavailable", media.PDF_UNAVAILABLE)
        try:
            text = await self.pdf.extract(prepared.data, policy.pdf_text_max_bytes)
        except PdfUnavailable as exc:
            raise media.AttachmentRefused(f"pdf_{exc}", "I couldn't read this PDF right now; send the text or a"
                                                        " screenshot") from exc  # fmt: skip
        if not text.strip():
            raise media.AttachmentRefused("pdf_no_text", "This PDF has no text layer; send a screenshot instead")
        prepared.parts.append(media.pdf_text_part(name, text, policy))
        prepared.needs_pdf_text = False
        return prepared
