"""RESCOPE_REQUIRED card (DESIGN §14.11.17; AC 39): the measured size vs the approved class as gatekeeper
facts, Continue at the measured class in two taps, Make it smaller, a read-only Explore and Drop."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from ossbot.common.clock import to_iso
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.proposals import ProposalRefused, ProposalService
from ossbot.gatekeeper.telegram import rescope_cards
from tests.conftest import insert_contribution, write_flag
from tests.telegram.proposalkit import state
from tests.telegram.reviewkit import labels, latest_send, sent_with, text, toast, token, view_message, write_patch
from tests.telegram.tgharness import TgRig, make_rig

REPORT: dict[str, Any] = {
    "approved_class": "s", "measured_class": "m", "estimate": {"code_lines": 35},
    "measured": {"files": 6, "code_added": 142, "code_deleted": 30, "test_added": 210, "test_deleted": 0},
    "outside_areas": ["web/src/config.ts", "docs/export.md"],
}  # fmt: skip


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def rescoped(ctx: AppContext, report: dict[str, Any] = REPORT, **cols: Any) -> str:
    return insert_contribution(ctx, State.RESCOPE_REQUIRED, size_report_json=json.dumps(report), proposal_version=1,
                               est_class="s", approved_class="s",
                               review_json=json.dumps({"size_reason": "the empty payload needs a config migration"}),
                               **cols)  # fmt: skip


async def send(rig: TgRig, cid: str, reason: rescope_cards.Reason = "overrun",
               report: dict[str, Any] = REPORT) -> int:  # fmt: skip
    rescope_cards.send(rig.ctx, cid, 1, reason, report)
    await rig.flush()
    return view_message(rig.ctx, "ceiling" if reason == "ceiling" else "rescope")


async def test_card_shows_gatekeeper_facts(rig: TgRig) -> None:
    cid = rescoped(rig.ctx)
    mid = await send(rig, cid)
    body = text(rig, mid)
    assert body.startswith(f"📏 <b>Re-approval needed</b> · {cid} · v1 → v2 · ")
    assert "n/a: local change" in body.split("\n", 1)[0]
    assert "Approved <b>s</b> (~35 code) · measured ✅ <b>m</b>: 6 files · +142 / −30 code · +210 test" in body
    assert "⚠️ 2 files outside the plan: web/src/config.ts, docs/export.md" in body
    assert '🤖 coder: "the empty payload needs a config migration"' in body
    assert labels(rig, mid) == ["✅ Continue at m", "📏 Make it smaller", "🧭 Explore", "🗑 Drop"]
    sent = latest_send(rig, mid)
    assert not sent.get("disable_notification")  # P1: a size decision rings


async def test_continue_is_two_taps_and_binds_the_measured_class(rig: TgRig) -> None:
    cid = rescoped(rig.ctx)
    mid = await send(rig, cid)
    await rig.tap(token(rig, mid, "✅ Continue at m"), message=mid)
    sentence = f"Confirm: continue {cid} at size m (was s). Local work only — dry-run does not apply."
    assert sentence in text(rig, mid)
    assert state(rig.ctx, cid) == "RESCOPE_REQUIRED"
    await rig.tap(token(rig, mid, "🛠 Confirm continue"), message=mid)
    row = db.one(rig.ctx.conn, "SELECT state, approved_class FROM contributions WHERE id = ?", (cid,))
    assert row is not None and (row["state"], row["approved_class"]) == ("PACKAGING", "m")


async def test_cancel_returns_to_the_card(rig: TgRig) -> None:
    cid = rescoped(rig.ctx)
    mid = await send(rig, cid)
    await rig.tap(token(rig, mid, "✅ Continue at m"), message=mid)
    await rig.tap(token(rig, mid, "↩️ Cancel"), message=mid)
    assert labels(rig, mid)[0] == "✅ Continue at m"


async def test_continue_refused_while_paused(rig: TgRig) -> None:
    cid = rescoped(rig.ctx)
    mid = await send(rig, cid)
    await rig.tap(token(rig, mid, "✅ Continue at m"), message=mid)
    rig.ctx.control.pause("test", "uid:1000/operator")
    await rig.tap(token(rig, mid, "🛠 Confirm continue"), message=mid)
    assert toast(rig).startswith("Paused or killed")
    assert state(rig.ctx, cid) == "RESCOPE_REQUIRED"


@pytest.mark.parametrize("scope", ["draining", "workers_down"])
async def test_continue_refused_while_draining_or_workers_down(rig: TgRig, scope: str) -> None:
    """§14.11.17: Continue is a proposal Start work, so the §14.11.15 refusals apply at both taps."""
    cid = rescoped(rig.ctx)
    mid = await send(rig, cid)
    await rig.tap(token(rig, mid, "✅ Continue at m"), message=mid)
    state_ = "draining" if scope == "draining" else "down"
    write_flag(rig.ctx.paths.lifecycle_flag, {"state": state_, "scope": "workers", "reason": "t", "actor": "tg:1",
                                              "ts": to_iso(rig.ctx.clock.now()), "timeout_s": 10})  # fmt: skip
    await rig.tap(token(rig, mid, "🛠 Confirm continue"), message=mid)
    what = "draining" if scope == "draining" else "down (workers)"
    assert toast(rig) == f"ossbot is {what}: nothing was approved. The card stays valid; retry after up."
    assert state(rig.ctx, cid) == "RESCOPE_REQUIRED"
    mid2 = await send(rig, cid)
    await rig.tap(token(rig, mid2, "✅ Continue at m"), message=mid2)  # the first tap is refused too
    assert toast(rig).startswith(f"ossbot is {what}")
    with pytest.raises(ProposalRefused):  # and the service itself, before the transition
        ProposalService(rig.ctx).continue_rescope(cid, "tg:4242", at_measured=True)


async def test_continue_binds_the_report_the_panel_showed(rig: TgRig) -> None:
    cid = rescoped(rig.ctx)
    mid = await send(rig, cid)
    await rig.tap(token(rig, mid, "✅ Continue at m"), message=mid)
    changed = {**REPORT, "measured_class": "l", "rev": 2}
    with db.transaction(rig.ctx.conn):  # the item left and re-entered RESCOPE_REQUIRED with a new report
        rig.ctx.conn.execute("UPDATE contributions SET size_report_json = ? WHERE id = ?", (json.dumps(changed), cid))
    await rig.tap(token(rig, mid, "🛠 Confirm continue"), message=mid)
    assert toast(rig).startswith("Superseded")
    row = db.one(rig.ctx.conn, "SELECT state, approved_class FROM contributions WHERE id = ?", (cid,))
    assert row is not None and (row["state"], row["approved_class"]) == ("RESCOPE_REQUIRED", "s")


async def test_continue_records_the_human_decision(rig: TgRig) -> None:
    cid = rescoped(rig.ctx)
    mid = await send(rig, cid)
    await rig.tap(token(rig, mid, "✅ Continue at m"), message=mid)
    await rig.tap(token(rig, mid, "🛠 Confirm continue"), message=mid)
    ev = db.one(rig.ctx.conn, "SELECT actor, data_json FROM audit_events WHERE cid = ? AND event = 'transition'"
                " ORDER BY seq DESC LIMIT 1", (cid,))  # fmt: skip
    assert ev is not None and ev["actor"] == "tg:4242"
    assert json.loads(ev["data_json"])["evidence"]["decided_by"] == "human"


@pytest.mark.parametrize(("approved", "target"), [("s", "s"), ("xs", "xs")])
async def test_make_it_smaller_sends_feedback(rig: TgRig, approved: str, target: str) -> None:
    """§14.11.17: back to the approved class (xs included), not one below it (F009-R2-4)."""
    cid = rescoped(rig.ctx, {**REPORT, "approved_class": approved})
    with db.transaction(rig.ctx.conn):
        rig.ctx.conn.execute("UPDATE contributions SET approved_class = ? WHERE id = ?", (approved, cid))
    mid = await send(rig, cid)
    await rig.tap(token(rig, mid, "📏 Make it smaller"), message=mid)
    fb = db.one(rig.ctx.conn, "SELECT proposal_feedback_json FROM contributions WHERE id = ?", (cid,))
    assert fb is not None and f"Reduce the scope to fit class {target};" in str(fb[0])
    assert "⛔ Revising v1 → v2…" in text(rig, mid)


async def test_drop_needs_a_confirm(rig: TgRig) -> None:
    cid = rescoped(rig.ctx)
    mid = await send(rig, cid)
    await rig.tap(token(rig, mid, "🗑 Drop"), message=mid)
    assert state(rig.ctx, cid) == "RESCOPE_REQUIRED"
    await rig.tap(token(rig, mid, "🗑 Drop it"), message=mid)
    assert state(rig.ctx, cid) == "ABANDONED"
    assert f"🗑 Dropped {cid}" in text(rig, mid)


async def test_explore_is_read_only(rig: TgRig) -> None:
    cid = rescoped(rig.ctx)
    write_patch(rig.ctx, cid, 1)
    mid = await send(rig, cid)
    await rig.tap(token(rig, mid, "🧭 Explore"), message=mid)
    ex = view_message(rig.ctx, "explorer")
    found = labels(rig, ex)
    assert not any(label.startswith(("💬", "⬇️ Show card", "✅")) for label in found)
    purposes = {r[0] for r in rig.ctx.conn.execute("SELECT purpose FROM ui_tokens WHERE tg_message_id = ?", (ex,))}
    assert purposes and not purposes & {"approve", "confirm", "proposal_start", "proposal_confirm"}


async def test_buttons_after_the_item_moved_on(rig: TgRig) -> None:
    cid = rescoped(rig.ctx)
    mid = await send(rig, cid)
    with db.transaction(rig.ctx.conn):
        rig.ctx.conn.execute("UPDATE contributions SET state = 'ABANDONED' WHERE id = ?", (cid,))
    await rig.tap(token(rig, mid, "✅ Continue at m"), message=mid)
    assert toast(rig) == rescope_cards.GONE
    assert sent_with(rig, "Re-approval needed") == mid
