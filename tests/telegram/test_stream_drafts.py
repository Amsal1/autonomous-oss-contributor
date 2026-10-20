"""Streaming assistant turns (DESIGN §14.12.1, AC 18, AC 48 drafts, U14): drafts with Stop, the draft
lane's pacing, the final persisted answer with sources and footer, and the no-secret-prefix rule."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ossbot.common.redact import REDACTED
from ossbot.gatekeeper import db
from ossbot.gatekeeper.chat import drafts
from ossbot.gatekeeper.chat.drafts import DraftContent, DraftLane
from ossbot.gatekeeper.chat.hermes_api import Event
from ossbot.gatekeeper.context import AppContext
from tests.telegram.chatkit import FakeBackend, deltas, wire
from tests.telegram.tgharness import OWNER, TgRig, make_rig

GHP = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def _turn(rig: TgRig) -> Any:
    return db.one(rig.ctx.conn, "SELECT * FROM chat_turns ORDER BY started_at DESC LIMIT 1")


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


async def test_a_turn_streams_drafts_then_one_final_message(rig: TgRig) -> None:
    events = [Event("tool", tool="web_search"),
              Event("tool_done", tool="web_fetch", data={"url": "https://developer.hashicorp.com/terraform"}),
              *deltas("Terraform is **declarative**.\n\nSee [docs](https://developer.hashicorp.com/terraform).",
                      model="claude-sonnet-4.5")]  # fmt: skip
    wire(rig.ctx, FakeBackend(events))
    await rig.say("what is terraform?")
    drafts_sent = [p for m, p in rig.req.calls if m in {"sendRichMessageDraft", "sendMessageDraft"}]
    assert drafts_sent, rig.req.methods()
    first = drafts_sent[0]
    turn = _turn(rig)
    assert first["draft_id"] == turn["turn_id"] and first["can_stop"] is True and first["keep_on_stop"] is False
    final = rig.sent("sendRichMessage")
    assert len(final) == 1
    doc = _dump(final[0])
    # the fetched URL and the answer's link are one source (deduplicated)
    assert "declarative" in doc and "Sources (1)" in doc and "🧠 claude-sonnet-4.5" in doc and "🔎 1" in doc, doc
    assert "📄 1" in doc and "🤖 Assistant · answer to your message" in doc  # flat mode: header + quote
    assert turn["status"] == "done" and "declarative" in turn["answer_text"]
    reactions = [p["reaction"][0]["emoji"] for m, p in rig.req.calls if m == "setMessageReaction"]
    assert reactions == ["👀", "👌"]


async def test_a_second_message_to_a_busy_session_is_not_relayed(rig: TgRig) -> None:
    backend = wire(rig.ctx, FakeBackend(deltas("partial"), hold=True))
    await rig.app.process_update(_update("first"))
    await backend.started.wait()
    await rig.app.process_update(_update("second"))
    await rig.flush()
    assert "⏳ Still answering — tap ⏹ Stop or wait" in rig.last_text()
    assert len(backend.calls) == 1
    await rig.handlers.drain()


def _update(text: str) -> Any:
    from telegram import Update
    from tests.fakes.fake_telegram import message_update

    return Update.de_json(message_update(OWNER, OWNER, text), None)


def test_draft_content_never_shows_a_secret_prefix() -> None:
    for cut_at in range(4, len(GHP) + 1):
        raw = f"The token is {GHP[:cut_at]}"
        out = drafts.content(raw, status="", heartbeat=None, wrapped=False, rich=True, header="h")
        assert GHP[:4] not in out.html and (out.rich is None or GHP[:4] not in _dump(out.rich.model_dump()))
    full = drafts.content(f"The token is {GHP} ok", status="", heartbeat=None, wrapped=False, rich=False, header="h")
    assert GHP not in full.html and REDACTED in full.html


@settings(max_examples=60, deadline=None)
@given(
    prefix=st.text(alphabet=st.characters(blacklist_characters="g"), max_size=300),
    n=st.integers(min_value=4, max_value=len(GHP)),
    wrapped=st.booleans(),
)
def test_no_prefix_of_a_secret_ever_reaches_a_draft(prefix: str, n: int, wrapped: bool) -> None:
    """U14: the trailing token is held back until it is complete (then redacted), so no prefix shows."""
    out = drafts.content(prefix + " " + GHP[:n], status="", heartbeat=None, wrapped=wrapped, rich=True, header="h")
    shown = out.html + (_dump(out.rich.model_dump()) if out.rich else "")
    assert GHP[:n] not in shown


def test_flat_mode_drafts_are_wrapped_like_the_answer() -> None:
    out = drafts.content("🧩 Ready for review ✅ CI passed", status="🔎 Searching the web (1)", heartbeat=None,
                         wrapped=True, rich=True, header="🤖 Assistant · answer to your message")  # fmt: skip
    assert out.rich is not None
    kinds = [b.type for b in out.rich.blocks]
    assert kinds[:2] == ["paragraph", "blockquote"]
    assert out.html.startswith("🤖 Assistant · answer to your message\n<blockquote expandable>")


def test_empty_draft_is_thinking() -> None:
    out = drafts.content("", status="", heartbeat=None, wrapped=False, rich=True, header="h")
    assert out.rich is not None and out.rich.blocks[0].type == "thinking"


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


async def test_lane_paces_drafts_yields_to_the_outbox_and_sends_heartbeats(monkeypatch: pytest.MonkeyPatch) -> None:
    DraftLane._last_by_chat.clear()
    clock = Clock()
    sent: list[tuple[str, float]] = []
    due = [False]

    async def send(method: str, payload: dict[str, Any]) -> None:
        sent.append((method, clock.t))

    built: list[bool] = []

    def build(heartbeat: bool) -> DraftContent:
        built.append(heartbeat)
        return drafts.content("x", status="", heartbeat="⏳ 45 s" if heartbeat else None, wrapped=False, rich=False,
                              header="h")  # fmt: skip

    lane = DraftLane(send, build, chat_id=1, thread_id=None, draft_id=7, rich=False, outbox_due=lambda: due[0],
                     monotonic=clock, min_interval_s=1.0, heartbeat_s=20.0)  # fmt: skip
    monkeypatch.setattr(drafts, "TICK_S", 0.0)
    task = asyncio.ensure_future(lane.run())
    await asyncio.sleep(0.01)
    assert [m for m, _ in sent] == ["sendMessageDraft"]  # immediately
    lane.touch()
    clock.t += 0.5
    await asyncio.sleep(0.01)
    assert len(sent) == 1  # at most one per second
    due[0] = True
    clock.t += 1.0
    await asyncio.sleep(0.01)
    assert len(sent) == 1  # yields while a card or alert is due
    due[0] = False
    await asyncio.sleep(0.01)
    assert len(sent) == 2 and built[-1] is False
    clock.t += 20.0
    await asyncio.sleep(0.01)
    assert len(sent) == 3 and built[-1] is True  # heartbeat with no new content
    lane.close()
    await task


async def test_a_refused_rich_draft_falls_back_to_classic_then_typing() -> None:
    DraftLane._last_by_chat.clear()
    calls: list[str] = []

    async def send(method: str, payload: dict[str, Any]) -> None:
        calls.append(method)
        raise RuntimeError("400")

    lane = DraftLane(send, lambda hb: drafts.content("x", status="", heartbeat=None, wrapped=False, rich=True,
                                                     header="h"), chat_id=1, thread_id=None, draft_id=7, rich=True,
                     outbox_due=lambda: False)  # fmt: skip
    await lane.send_once()
    assert calls == ["sendRichMessageDraft", "sendMessageDraft"] and lane.mode == "typing"
