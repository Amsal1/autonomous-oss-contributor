"""Hermes API backend (DESIGN §15.6, §14.12; PLAN item 37): SSE parsing, session keys, request roles,
whole turns through ``ChatService`` and cancel."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from ossbot.common.control_flags import FlagState, HermesTier
from ossbot.gatekeeper.chat import hermes_api
from ossbot.gatekeeper.chat.backend import ChatService, HermesApiBackend, RelayClosed, RelayGate
from ossbot.gatekeeper.chat.hermes_api import HermesClient, HermesError, HermesUnavailable
from ossbot.gatekeeper.chat.hermes_surface import SurfaceResult
from ossbot.gatekeeper.chat.turns import TurnStore
from ossbot.gatekeeper.context import AppContext
from tests.fakes.fake_hermes import FakeHermesApi, answer_events, completed, sse

PASSED = {"passed": True, "checked_at": "2026-10-19T12:00:00Z"}
NOW = datetime(2026, 10, 19, 12, 10, tzinfo=UTC).timestamp()  # 10 min after PASSED


def gate(*, surface: SurfaceResult | None = None) -> RelayGate:
    return RelayGate(
        flags=lambda: FlagState(None, None), lifecycle=lambda: None, probe_doc=lambda: PASSED,
        surface=lambda t: surface or SurfaceResult("ok"), tier=lambda: "self_improve", on_surface_kill=lambda r: None,
        now=lambda: NOW,
    )  # fmt: skip


def backend(api: FakeHermesApi, g: RelayGate | None = None) -> HermesApiBackend:
    return HermesApiBackend(HermesClient("hk_test", transport=api.transport()), g or gate())


async def collect(client: HermesClient, parts: list[dict[str, Any]], key: str = "tg-1-1", **kw: Any) -> list[Any]:
    return [e async for e in client.stream(parts, key, **kw)]


async def test_stream_parses_deltas_tools_and_the_final_answer() -> None:
    api = FakeHermesApi(events=answer_events(["Hel", "lo"], tools=["web_search", "ossbot_status"]))
    events = await collect(HermesClient("hk_test", transport=api.transport()), [{"type": "input_text", "text": "hi"}])
    kinds = [e.kind for e in events]
    assert kinds == ["tool", "tool", "delta", "delta", "completed"]
    assert [e.tool for e in events if e.kind == "tool"] == ["web_search", "ossbot_status"]
    assert hermes_api.final_text(events[-1].data) == "Hello"
    req = api.requests[0]
    assert req["path"] == "/v1/responses" and req["headers"]["x-hermes-session-key"] == "tg-1-1"
    assert req["headers"]["authorization"] == "Bearer hk_test"
    assert req["body"]["stream"] is True and req["body"]["conversation"] == "tg-1-1"
    assert req["body"]["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
    assert "model" not in req["body"] and "provider" not in req["body"]


def test_final_text_skips_commentary_and_non_text_parts() -> None:
    doc = hermes_api.parse_event("", completed("answer", commentary="thinking…").split("data: ", 1)[1])
    assert doc is not None and hermes_api.final_text(doc.data) == "answer"
    assert hermes_api.final_text({"output": [{"type": "message", "content": [{"type": "refusal"}, 5]}, "x"]}) == ""


IGNORED = ["not json", "[1]", '{"type": "response.unknown"}',
           '{"type": "response.output_item.done", "item": {"type": "function_call"}}']  # fmt: skip


@pytest.mark.parametrize("data", IGNORED)
def test_unknown_or_malformed_events_are_ignored(data: str) -> None:
    assert hermes_api.parse_event("", data) is None


def test_failed_event_keeps_only_a_bounded_message() -> None:
    doc = {"type": "response.failed", "response": {"error": {"message": "x" * 900}}}
    event = hermes_api.parse_event("", json.dumps(doc))
    assert event is not None and event.kind == "failed" and len(event.text) == 300


async def test_oversized_event_is_an_error() -> None:
    big = "data: " + "a" * (hermes_api.MAX_EVENT + 10) + "\n\n"
    api = FakeHermesApi(events=[big])
    with pytest.raises(HermesError, match="size limit"):
        await collect(HermesClient("hk_test", transport=api.transport()), [])


async def test_http_error_and_refused_connection() -> None:
    api = FakeHermesApi(status=400)
    with pytest.raises(HermesError, match="HTTP 400"):
        await collect(HermesClient("hk_test", transport=api.transport()), [])
    with pytest.raises(HermesUnavailable):
        await collect(HermesClient("hk_test", transport=FakeHermesApi(connect_error=True).transport()), [])


@pytest.mark.parametrize("key", ["", "a\nb", "x" * 300])
async def test_bad_session_keys_are_refused(key: str) -> None:
    with pytest.raises(ValueError, match="session key"):
        await collect(HermesClient("hk_test", transport=FakeHermesApi().transport()), [], key)


async def test_request_role_is_sent_as_a_role_model_for_the_kiro_provider() -> None:
    api = FakeHermesApi()
    events = [e async for e in backend(api).stream([], "selftest-1", request_role="test")]
    assert events[-1].kind == "completed"
    assert api.requests[0]["body"]["model"] == "role:test" and api.requests[0]["body"]["provider"] == "kiro-acp"


async def test_closed_gate_sends_nothing() -> None:
    api = FakeHermesApi()
    g = RelayGate(flags=lambda: FlagState(None, None), lifecycle=lambda: "draining", probe_doc=lambda: PASSED,
                  surface=lambda t: SurfaceResult("ok"), tier=lambda: "locked", on_surface_kill=lambda r: None,
                  now=lambda: NOW)  # fmt: skip
    with pytest.raises(RelayClosed) as info:
        [e async for e in backend(api, g).stream([], "tg-1-1")]
    assert info.value.gate.reason == "workers_down" and api.requests == []


async def test_unavailable_hermes_invalidates_the_cached_surface() -> None:
    calls: list[HermesTier] = []

    def surface(t: HermesTier) -> SurfaceResult:
        calls.append(t)
        return SurfaceResult("ok")

    g = RelayGate(flags=lambda: FlagState(None, None), lifecycle=lambda: None, probe_doc=lambda: PASSED,
                  surface=surface, tier=lambda: "locked", on_surface_kill=lambda r: None, now=lambda: NOW)  # fmt: skip
    b = backend(FakeHermesApi(connect_error=True), g)
    with pytest.raises(HermesUnavailable):
        [e async for e in b.stream([], "tg-1-1")]
    assert (await g.check()).open and len(calls) == 2  # re-checked, not served from the cache


# ------------------------------------------------------------------ ChatService (whole turns)
def service(ctx: AppContext, api: FakeHermesApi) -> ChatService:
    return ChatService(backend(api), TurnStore(ctx.conn, ctx.clock))


async def test_ask_stores_the_redacted_answer(ctx: AppContext) -> None:
    secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    api = FakeHermesApi(events=answer_events(["The token is ", secret], tools=["web_search"]))
    out = await service(ctx, api).ask([{"type": "input_text", "text": "q"}], "tg-1-1", user_message_id=7,
                                      model_label="claude-haiku-4.5")  # fmt: skip
    assert out.status == "done" and secret not in out.answer and out.tools == ["web_search"]
    assert out.streamed_deltas == 2 and out.response_id == "resp_1"
    row = ctx.conn.execute("SELECT * FROM chat_turns WHERE turn_id = ?", (out.turn_id,)).fetchone()
    assert row["status"] == "done" and row["answer_text"] == out.answer and row["model"] == "claude-haiku-4.5"
    assert row["user_message_id"] == 7 and row["run_id"] == "resp_1" and secret not in row["answer_text"]


async def test_ask_uses_the_deltas_when_completed_has_no_text(ctx: AppContext) -> None:
    done = {"type": "response.completed", "response": {"id": "r", "output": []}}
    events = [sse("response.output_text.delta", {"type": "response.output_text.delta", "delta": "only deltas"}),
              sse("response.completed", done)]  # fmt: skip
    out = await service(ctx, FakeHermesApi(events=events)).ask([], "tg-1-1")
    assert out.answer == "only deltas"


async def test_failed_event_marks_the_turn_failed(ctx: AppContext) -> None:
    events = [sse("response.failed", {"type": "response.failed", "response": {"error": {"message": "model down"}}})]
    out = await service(ctx, FakeHermesApi(events=events)).ask([], "tg-1-1")
    assert out.status == "failed" and out.error == "model down"
    assert TurnStore(ctx.conn, ctx.clock).status(out.turn_id) == "failed"


async def test_closed_gate_writes_no_turn(ctx: AppContext) -> None:
    svc = ChatService(backend(FakeHermesApi(), gate(surface=SurfaceResult("unavailable", "refused"))),
                      TurnStore(ctx.conn, ctx.clock))  # fmt: skip
    with pytest.raises(RelayClosed):
        await svc.ask([], "tg-1-1")
    assert ctx.conn.execute("SELECT COUNT(*) FROM chat_turns").fetchone()[0] == 0


class _HangingBackend:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def gate(self) -> Any:
        return await gate().check()

    async def stream(self, parts: Any, session_key: str, *, request_role: str | None = None) -> Any:
        self.started.set()
        await asyncio.sleep(60)
        yield  # pragma: no cover


async def test_cancel_marks_the_turn_stopped(ctx: AppContext) -> None:
    hang = _HangingBackend()
    turns = TurnStore(ctx.conn, ctx.clock)
    task = asyncio.create_task(ChatService(hang, turns).ask([], "tg-1-1"))
    await hang.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = ctx.conn.execute("SELECT status FROM chat_turns").fetchone()
    assert row["status"] == "stopped" and not turns.busy("tg-1-1")
