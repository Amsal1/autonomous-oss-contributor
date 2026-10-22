"""Ceiling-hit card (DESIGN §14.11.18; AC 39, AC 62, U13): measured vs the configured ceiling, read-only;
Split sends the fixed feedback at once plus an optional ForceReply; Re-check re-runs only the size check;
no button raises the ceiling."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.proposals import SPLIT_TEXT
from ossbot.gatekeeper.telegram import rescope_cards
from tests.telegram.proposalkit import state
from tests.telegram.reviewkit import labels, last_sent_id, latest_send, text, toast, token
from tests.telegram.test_rescope_card import rescoped, send
from tests.telegram.tgharness import TgRig, make_rig

OVER: dict[str, Any] = {
    "approved_class": "s", "measured_class": "xxl",
    "measured": {"files": 91, "code_added": 3180, "code_deleted": 240, "test_added": 1020, "test_deleted": 0},
}  # fmt: skip


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


async def test_card_is_read_only_facts(rig: TgRig) -> None:
    cid = rescoped(rig.ctx, OVER)
    mid = await send(rig, cid, "ceiling", OVER)
    body = text(rig, mid)
    c = rig.ctx.config.policy.diff
    assert body.startswith(f"🧱 <b>Size ceiling reached</b> · <code>{cid}</code> · paused")
    assert "Measured ✅ 91 files · +3,180 / −240 code · +1,020 test" in body
    ceiling = f"{c.max_files} files · {c.max_changed_lines:,} code · {c.max_test_lines:,} test"
    assert f"Ceiling (config only): {ceiling}" in body
    assert rescope_cards.GUARD in body
    assert labels(rig, mid) == ["✂️ Split (adjust plan)", "🗑 Drop", "🔁 Re-check", "🧭 Explore"]
    # U13: nothing on the card can raise the ceiling
    purposes = {r[0] for r in rig.ctx.conn.execute("SELECT purpose FROM ui_tokens WHERE tg_message_id = ?", (mid,))}
    assert purposes == {"rescope_split", "rescope_drop", "rescope_recheck", "rescope_explore"}


async def test_split_sends_feedback_then_an_optional_prompt(rig: TgRig) -> None:
    cid = rescoped(rig.ctx, OVER)
    mid = await send(rig, cid, "ceiling", OVER)
    await rig.tap(token(rig, mid, "✂️ Split"), message=mid)
    fb = db.one(rig.ctx.conn, "SELECT proposal_feedback_json FROM contributions WHERE id = ?", (cid,))
    assert fb is not None and SPLIT_TEXT in str(fb[0])
    assert "⛔ Revising v1 → v2…" in text(rig, mid)
    prompt = last_sent_id(rig)
    sent = latest_send(rig, prompt)
    assert sent["text"].startswith("✂️ Split requested.")
    assert sent["reply_markup"]["input_field_placeholder"] == "Anything to add? (optional)"
    assert len(rescope_cards.SPLIT_PLACEHOLDER) <= 64
    pending = db.one(rig.ctx.conn, "SELECT kind FROM pending_input WHERE prompt_message_id = ?", (prompt,))
    assert pending is not None and pending["kind"] == "proposal_adjust"


async def test_recheck_still_over_then_fits(rig: TgRig, monkeypatch: pytest.MonkeyPatch) -> None:
    cid = rescoped(rig.ctx, OVER)
    mid = await send(rig, cid, "ceiling", OVER)
    await rig.tap(token(rig, mid, "🔁 Re-check"), message=mid)
    assert toast(rig) == f"Still over the ceiling (91 > {rig.ctx.config.policy.diff.max_files} files)"
    assert state(rig.ctx, cid) == "RESCOPE_REQUIRED"
    monkeypatch.setattr(rescope_cards, "_over", lambda ctx, report: None)  # as after a raised ceiling + /reload
    await rig.tap(token(rig, mid, "🔁 Re-check"), message=mid)
    assert toast(rig) == "Within the ceiling now."
    assert state(rig.ctx, cid) == "PACKAGING"
