"""Reminders (DESIGN §14.19 item 3, U12): ``/remind`` and Hermes ``ossbot_reminder_create`` fire in the
thread they were made in as P1, wait out quiet hours, list and cancel, ≤ 50 active."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest

from ossbot.common.clock import FakeClock
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import reminders
from tests.conftest import UID_REQUESTER
from tests.telegram.tgharness import TgRig, make_rig
from tests.unit.gatekeeper.api_helpers import client


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def _rows(ctx: AppContext) -> list[dict[str, object]]:
    return [dict(r) for r in ctx.conn.execute("SELECT * FROM reminders").fetchall()]


async def test_remind_creates_lists_fires_once_and_cancels(rig: TgRig, clock: FakeClock) -> None:
    await rig.say("/remind in 2h re-check PR #5678")
    assert "⏰ Reminder set for" in rig.last_text()
    await rig.say("/remind tomorrow 09:00 <b>stand-up</b>")
    await rig.say("/remind")
    listing = rig.sent()[-1]
    shown = listing["text"].replace("\u2060", "")  # the U8 defusing marker after "/"
    assert "re-check PR #5678" in shown and "&lt;b&gt;stand-up&lt;/b&gt;" in shown
    clock.advance(timedelta(hours=2, minutes=1))
    assert reminders.fire_due(rig.ctx) == 1 and reminders.fire_due(rig.ctx) == 0
    await rig.flush()
    fired = rig.sent()[-1]
    assert fired["text"] == "⏰ Reminder: re-check PR #5678"
    row = rig.ctx.conn.execute("SELECT priority FROM tg_outbox ORDER BY id DESC LIMIT 1").fetchone()
    assert row["priority"] == 1
    await rig.say("/remind")
    token = rig.token_of(rig.sent()[-1], "🗑 Cancel 1")
    await rig.tap(token, message=rig.message_id_of(-1))
    assert all(r["cancelled_at"] or r["fired_at"] for r in _rows(rig.ctx))


@pytest.mark.parametrize("args", ["/remind soon do it", "/remind in 2h", "/remind 2000-01-01 09:00 past"])
async def test_bad_reminders_are_refused(rig: TgRig, args: str) -> None:
    await rig.say(args)
    assert rig.last_text().startswith("⏰ ") and "set for" not in rig.last_text()
    assert _rows(rig.ctx) == []


def test_at_most_50_active(ctx: AppContext, clock: FakeClock) -> None:
    for i in range(reminders.ACTIVE_MAX):
        reminders.create(ctx, clock.now() + timedelta(hours=1), f"r{i}", created_by="tg:4242", thread_id=None)
    with pytest.raises(reminders.RemindRefused, match="at most 50"):
        reminders.create(ctx, clock.now() + timedelta(hours=1), "one more", created_by="tg:4242", thread_id=None)


async def test_quiet_hours_hold_reminders_until_they_end(rig: TgRig, clock: FakeClock) -> None:
    from ossbot.gatekeeper.telegram.runtime import ui_of

    reminders.create(rig.ctx, clock.now() + timedelta(minutes=1), "drink water", created_by="tg:4242", thread_id=None)
    ui_of(rig.ctx).prefs.set("quiet_until", (clock.now() + timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
    clock.advance(timedelta(minutes=5))
    assert reminders.fire_due(rig.ctx) == 0
    clock.advance(timedelta(hours=1))
    assert reminders.fire_due(rig.ctx) == 1


async def test_hermes_reminder_needs_an_active_turn_and_starts_with_robot(ctx: AppContext, clock: FakeClock) -> None:
    assert ctx.chat is not None
    body = {"when": (clock.now() + timedelta(hours=1)).isoformat(), "text": "re-check the CI"}
    async with client(ctx, UID_REQUESTER) as c:
        assert (await c.post("/v1/reminders", json=body, headers={"X-Hermes-Session-Key": "tg-4242-1"})
                ).status_code == 409  # fmt: skip
        ctx.chat.service.turns.start("tg-4242-1", 1)
        resp = await c.post("/v1/reminders", json=body, headers={"X-Hermes-Session-Key": "tg-4242-1"})
        assert resp.status_code == 201
    clock.advance(timedelta(hours=1, minutes=1))
    assert reminders.fire_due(ctx) == 1
    row = ctx.conn.execute("SELECT payload_json, thread_key FROM tg_outbox ORDER BY id DESC LIMIT 1").fetchone()
    assert "🤖 ⏰ Reminder: re-check the CI" in row["payload_json"] and row["thread_key"] == "assistant"
