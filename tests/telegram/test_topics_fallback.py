"""Topics with the flat fallback (DESIGN §14.10.1, D1, AC 12)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.runtime import ui_of
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx, topics=True):
        yield r


def _topics(ctx: AppContext) -> dict[str, int]:
    return {r["name"]: int(r["thread_id"]) for r in ctx.conn.execute("SELECT * FROM tg_topics")}


async def test_system_topics_created_once_and_reused(ctx: AppContext, rig: TgRig) -> None:
    topics = _topics(ctx)
    assert set(topics) == {"control", "reviews", "feed", "assistant"}
    assert ui_of(ctx).topics.mode == "topics"
    await rig.frontend.workspace()  # a restart: the stored ids are reused, nothing is created again
    assert "createForumTopic" not in rig.req.methods()
    assert _topics(ctx) == topics


async def test_rows_go_to_their_topic_and_replies_to_their_origin(ctx: AppContext, rig: TgRig) -> None:
    topics = _topics(ctx)
    ui_send.deliver(ctx, k.plain("fyi", k.line("a card-like note")), thread="reviews")
    await rig.flush()
    assert rig.sent()[-1]["message_thread_id"] == topics["reviews"]
    await rig.say("/status", thread_id=topics["control"])
    assert rig.sent()[-1]["message_thread_id"] == topics["control"]
    await rig.say("/help", thread_id=4711)  # a user topic: the reply stays there
    assert rig.sent()[-1]["message_thread_id"] == 4711


async def test_thread_not_found_recreates_once_then_flat(ctx: AppContext, rig: TgRig) -> None:
    old = _topics(ctx)["reviews"]
    rig.req.fail("sendMessage", "Bad Request: message thread not found")
    ui_send.deliver(ctx, k.plain("fyi", k.line("first")), thread="reviews")
    await rig.flush()
    new = _topics(ctx)["reviews"]
    assert new != old and rig.req.methods().count("createForumTopic") == 1
    assert rig.sent()[-1]["message_thread_id"] == new and rig.sent()[-1]["text"].endswith("first")
    rig.req.fail("sendMessage", "Bad Request: message thread not found")
    ui_send.deliver(ctx, k.plain("fyi", k.line("second")), thread="reviews")
    await rig.flush()  # the second miss of this start: flat mode, and the message is still delivered
    assert rig.req.methods().count("createForumTopic") == 1
    assert ui_of(ctx).topics.mode == "flat"
    last = rig.sent()[-1]
    assert "message_thread_id" not in last and last["text"] == "✅ second"
    assert db.one(ctx.conn, "SELECT COUNT(*) FROM tg_outbox WHERE status = 'failed'")[0] == 0


async def test_flat_mode_without_topic_mode(ctx: AppContext) -> None:
    async for rig in make_rig(ctx, topics=False):
        assert ui_of(ctx).topics.mode == "flat"
        assert "createForumTopic" not in rig.req.methods()
        ui_send.deliver(ctx, k.plain("fyi", k.line("hello")), thread="feed")
        await rig.flush()
        assert rig.sent()[-1]["text"] == "📈 hello"  # the section icon replaces the topic


async def test_topics_off_in_config(ctx: AppContext) -> None:
    path = ctx.paths.config_dir / "telegram.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("topics: auto", "topics: off"), encoding="utf-8")
    ctx.snapshots.load_at_start()
    assert ctx.config.telegram.topics == "off"
    async for rig in make_rig(ctx, topics=True):
        assert ui_of(ctx).topics.mode == "flat"
        assert "createForumTopic" not in rig.req.methods()
