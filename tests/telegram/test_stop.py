"""Stop (DESIGN §14.12.1 step 5, AC 18, AC 59 Stop part, U10, U14): ``stopped_message_generation`` for
the owner's active draft ends the stream within 2 s and persists ``draft_visible(partial) + "…"`` with
"⏹ Stopped by you"; any other chat or an unknown draft changes nothing."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from telegram import Update
from tests.fakes.fake_telegram import message_update, stopped_update
from tests.telegram.chatkit import FakeBackend, deltas, wire
from tests.telegram.tgharness import OWNER, TgRig, make_rig

GHP = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


async def _streaming(rig: TgRig, *parts: str) -> tuple[FakeBackend, int]:
    backend = wire(rig.ctx, FakeBackend(deltas(*parts), hold=True))
    await rig.app.process_update(Update.de_json(message_update(OWNER, OWNER, "explain"), rig.app.bot))
    await backend.started.wait()
    await asyncio.sleep(0.05)
    row = db.one(rig.ctx.conn, "SELECT turn_id FROM chat_turns WHERE status = 'streaming'")
    assert row is not None
    return backend, int(row["turn_id"])


async def _update(rig: TgRig, data: dict[str, Any]) -> None:
    await rig.app.process_update(Update.de_json(data, rig.app.bot))


async def test_stop_ends_the_stream_within_2s_and_persists_the_partial(rig: TgRig) -> None:
    backend, turn_id = await _streaming(rig, "The first part is long enough ", "and the key is ", GHP[:12])
    started = time.monotonic()
    await _update(rig, stopped_update(OWNER, turn_id))
    await asyncio.wait_for(backend.closed.wait(), 2.0)
    await rig.settle()
    assert time.monotonic() - started < 2.0
    await rig.flush()
    row = db.one(rig.ctx.conn, "SELECT * FROM chat_turns WHERE turn_id = ?", (turn_id,))
    assert row["status"] == "stopped" and row["answer_text"].endswith("…")
    assert "ghp_" not in row["answer_text"]  # the trailing incomplete token was held back (U14)
    final = json.dumps([p for m, p in rig.req.calls if m in {"sendRichMessage", "sendMessage"}], ensure_ascii=False)
    assert "⏹ Stopped by you" in final and "ghp_" not in final
    assert not rig.handlers.session_busy(f"tg-{OWNER}-1")


@pytest.mark.parametrize(("chat_id", "chat_type"), [(OWNER, "group"), (999, "private")])
async def test_stop_from_another_chat_is_ignored(rig: TgRig, chat_id: int, chat_type: str) -> None:
    backend, turn_id = await _streaming(rig, "still going")
    await _update(rig, stopped_update(chat_id, turn_id, chat_type))
    await asyncio.sleep(0.2)
    assert not backend.closed.is_set() and rig.handlers.session_busy(f"tg-{OWNER}-1")
    await rig.handlers.drain()


async def test_stop_for_an_unknown_draft_is_ignored(rig: TgRig) -> None:
    backend, turn_id = await _streaming(rig, "still going")
    await _update(rig, stopped_update(OWNER, turn_id + 1))
    await asyncio.sleep(0.2)
    assert not backend.closed.is_set()
    await rig.handlers.drain()
    row = db.one(rig.ctx.conn, "SELECT status FROM chat_turns WHERE turn_id = ?", (turn_id,))
    assert row["status"] == "stopped"  # the drain (a kill) cancelled it: finalize_turn closed the row
