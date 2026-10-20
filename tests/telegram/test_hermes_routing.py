"""Where unsolicited Hermes messages go (DESIGN §14.12.7 U12, AC 35, AC 48, BA-22).

Notifications and feature cards attach only to an active turn of the plugin's session key; a Reviews
session's messages land in 💬 Assistant; nothing unsolicited reaches Reviews, Control or OSS feed; each
carries 🤖 in its header line; the sixth Hermes P1 of a day is P2; P0 is a 422; worker notices land in
📈 OSS feed as P2. A stuck ``streaming`` row counts for 30 min, a finished one for 10 min, a row that
``reset_streaming`` failed never.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from ossbot.common.clock import FakeClock
from ossbot.gatekeeper import features
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.runtime import ui_of
from ossbot.gatekeeper.telegram.ui import kit as k
from tests.conftest import UID_REQUESTER, UID_WORKER
from tests.unit.gatekeeper.api_helpers import client

KEY = "tg-4242-1"
HDR = {"X-Hermes-Session-Key": KEY}


class Meal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: Annotated[str, Field(min_length=1, max_length=40)]
    kcal: Annotated[int, Field(ge=0, le=5000)]


def meal_card(data: BaseModel, prefs: Any) -> k.View:
    assert isinstance(data, Meal)
    head = (k.Const("🍽 "), k.Bold(k.Const("Meal logged")), k.Const(" · lunch · 🤖 assistant"))
    return k.View(kind="feature", blocks=[k.Block((k.Lines((head, (k.Text(data.item, 40), k.Const(f" · {data.kcal}"
                  " kcal")))),))], rows=[[k.button("↩️ Undo", "dash_refresh")]])  # fmt: skip


@pytest.fixture
def meals(ctx: AppContext) -> Iterator[None]:
    features.register(features.FeatureSpec("nutrition", 1, {"meal": Meal}, {"meal": meal_card}))
    ctx.config.telegram.features.enabled.append("nutrition")
    yield
    features.REGISTRY.pop("nutrition", None)
    ctx.config.telegram.features.enabled.remove("nutrition")


def _turn(ctx: AppContext, *, thread_id: int | None = None, done: bool = False, key: str = KEY) -> int:
    assert ctx.chat is not None
    turns = ctx.chat.service.turns
    turn_id = turns.start(key, 1, thread_id=thread_id)
    if done:
        turns.done(turn_id, "answer", model=None, tools=[])
    return turn_id


def _rows(ctx: AppContext) -> list[dict[str, Any]]:
    return [dict(r) | {"payload": json.loads(r["payload_json"])}
            for r in ctx.conn.execute("SELECT * FROM tg_outbox ORDER BY id").fetchall()]  # fmt: skip


async def _notify(ctx: AppContext, body: dict[str, Any], *, uid: int = UID_REQUESTER,
                  headers: dict[str, str] | None = None) -> Any:  # fmt: skip
    async with client(ctx, uid) as c:
        return await c.post("/v1/notify", json=body, headers=HDR if headers is None else headers)


async def test_unknown_or_missing_session_key_is_409_and_sends_nothing(ctx: AppContext) -> None:
    _turn(ctx)
    for headers in ({}, {"X-Hermes-Session-Key": "tg-4242-9"}):
        resp = await _notify(ctx, {"text": "hi"}, headers=headers)
        assert resp.status_code == 409 and resp.json() == {"error": "no_active_turn"}
    assert _rows(ctx) == []


@pytest.mark.parametrize(("minutes", "accepted"), [(29, True), (31, False)])
async def test_a_stuck_streaming_row_counts_for_30_minutes(ctx: AppContext, clock: FakeClock, minutes: int,
                                                           accepted: bool) -> None:  # fmt: skip
    _turn(ctx)
    clock.advance(timedelta(minutes=minutes))
    assert (await _notify(ctx, {"text": "hi"})).status_code == (202 if accepted else 409)


@pytest.mark.parametrize(("minutes", "accepted"), [(9, True), (11, False)])
async def test_a_finished_turn_counts_for_10_minutes(ctx: AppContext, clock: FakeClock, minutes: int,
                                                     accepted: bool) -> None:  # fmt: skip
    _turn(ctx, done=True)
    clock.advance(timedelta(minutes=minutes))
    assert (await _notify(ctx, {"text": "hi"})).status_code == (202 if accepted else 409)


async def test_a_row_reset_streaming_failed_is_never_accepted(ctx: AppContext) -> None:
    _turn(ctx)
    assert ctx.chat is not None and ctx.chat.service.turns.reset_streaming() == 1
    assert (await _notify(ctx, {"text": "hi"})).status_code == 409


async def test_p0_is_422_and_the_sixth_p1_of_a_day_is_p2(ctx: AppContext) -> None:
    _turn(ctx)
    assert (await _notify(ctx, {"text": "x", "priority": "P0"})).status_code == 422
    got = [(await _notify(ctx, {"text": f"n{i}", "priority": "P1"})).json()["priority"] for i in range(6)]
    assert got == ["P1"] * 5 + ["P2"]
    assert [r["priority"] for r in _rows(ctx)] == [1] * 5 + [2]
    assert ctx.audit.count_since("hermes_p1_demoted", "2000-01-01T00:00:00Z") == 1


async def test_flat_mode_notice_has_the_header_and_one_quote(ctx: AppContext) -> None:
    _turn(ctx)
    await _notify(ctx, {"text": "🧩 Ready for review ✅ CI passed"})
    (row,) = _rows(ctx)
    text = row["payload"].get("text") or row["payload"]["_classic"]["text"]
    assert text.startswith("🤖 <b>Assistant</b> · notice\n<blockquote expandable>🤖 🧩 Ready for review")


def _topics(ctx: AppContext) -> None:
    for name, tid in (("control", 10), ("reviews", 11), ("feed", 12), ("assistant", 13)):
        ctx.conn.execute("INSERT INTO tg_topics (name, thread_id, created_at) VALUES (?, ?, 'x')", (name, tid))
    ui_of(ctx).topics.mode = "topics"


@pytest.mark.parametrize(("thread", "lands", "first"), [(11, "assistant", "🤖 from your Reviews chat"),
                                                        (13, "assistant", "🤖 "), (77, None, "🤖 ")])  # fmt: skip
async def test_topic_mode_never_lands_in_reviews_control_or_feed(ctx: AppContext, thread: int, lands: str | None,
                                                                 first: str) -> None:  # fmt: skip
    _topics(ctx)
    _turn(ctx, thread_id=thread, key="tg-4242-t1")
    assert (await _notify(ctx, {"text": "reminder"}, headers={"X-Hermes-Session-Key": "tg-4242-t1"})).status_code == 202
    (row,) = _rows(ctx)
    assert row["thread_key"] == lands
    if lands is None:
        assert row["payload"]["message_thread_id"] == thread  # a user chat topic
    text = row["payload"].get("text") or row["payload"]["_classic"]["text"]
    assert text.startswith(first)


async def test_worker_notice_lands_in_the_feed_as_p2(ctx: AppContext) -> None:
    assert (await _notify(ctx, {"text": "stage done"}, uid=UID_WORKER, headers={})).status_code == 202
    (row,) = _rows(ctx)
    assert row["thread_key"] == "feed" and row["priority"] == 2 and row["payload"]["text"] == "ℹ️ stage done"


async def test_feature_card_routes_like_a_notice_and_counts_on_the_turn(ctx: AppContext, meals: None) -> None:
    turn_id = _turn(ctx, done=True)
    async with client(ctx, UID_REQUESTER) as c:
        body = {"kind": "meal", "data": {"item": "chicken leg", "kcal": 320}}
        assert (await c.post("/v1/features/nutrition/cards", json=body, headers=HDR)).status_code == 202
        bad = await c.post(
            "/v1/features/nutrition/cards",
            json={"kind": "meal", "data": {"item": "x", "kcal": -1, "extra": 1}},
            headers=HDR,
        )
        assert bad.status_code == 422 and "-1" not in bad.text
        assert (await c.post("/v1/features/nutrition/cards", json=body, headers={})).status_code == 409
        assert (await c.post("/v1/features/unknown/cards", json=body, headers=HDR)).status_code == 404
    (row,) = _rows(ctx)
    text = row["payload"].get("text") or row["payload"]["_classic"]["text"]
    assert text.startswith("🤖 <b>Assistant</b> · nutrition\n<blockquote expandable>🍽 <b>Meal logged</b>")
    assert row["bind_tokens_json"]  # the feature's buttons stay
    assert ctx.chat is not None
    assert ctx.chat.service.turns.row(turn_id)["feature_cards"] == 1  # type: ignore[index]
