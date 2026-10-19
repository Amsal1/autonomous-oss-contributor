"""S8 Confirm, S9 submitted and the Reject reasons (DESIGN §14.11.9-§14.11.10; mockup 03; AC 6)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from tests.conftest import packaged_contribution
from tests.telegram.reviewkit import (
    Card,
    card_of,
    labels,
    last_sent_id,
    latest,
    newest_approval,
    open_card,
    publish,
    status,
    text,
    token,
    view_message,
)
from tests.telegram.tgharness import TgRig, make_rig

PR_URL = "https://github.com/langfuse/langfuse/pull/77"


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async def submit(approval_id: str) -> str:
        live = not db.one(ctx.conn, "SELECT dry_run_shown FROM approvals WHERE id = ?", (approval_id,))[0]
        with db.transaction(ctx.conn):
            ctx.conn.execute("UPDATE outbound_actions SET status = ? WHERE id = (SELECT action_id FROM approvals"
                             " WHERE id = ?)", ("done" if live else "dry_run_done", approval_id))  # fmt: skip
            if live:
                ctx.conn.execute("UPDATE contributions SET pr_number = 77, pr_url = ? WHERE id = (SELECT o.cid FROM"
                                 " approvals a JOIN outbound_actions o ON o.id = a.action_id WHERE a.id = ?)",
                                 (PR_URL, approval_id))  # fmt: skip
        return "done" if live else "dry_run_done"

    async for r in make_rig(ctx, submit=submit):
        yield r


def _button(rig: TgRig, mid: int, label: str) -> dict[str, Any]:
    return next(b for row in rig.buttons(latest(rig, mid)) for b in row if b["text"] == label)


async def test_dry_run_panel_then_s9(rig: TgRig) -> None:
    card = await open_card(rig)
    await rig.tap(token(rig, card.message_id, "✅ Approve"), message=card.message_id)
    panel = text(rig, card.message_id)
    assert "🧪 <b>DRY RUN — nothing will be published</b>" in panel
    assert "<b>Confirm: DRY RUN — nothing will be published. Expires in 5 min.</b>" in panel
    assert "Reviewed: summary — · files 0/2 ⚠️" in panel
    assert "PR title: <code>Fix the thing</code>" in panel
    assert labels(rig, card.message_id) == ["🧪 Confirm dry run", "↩️ Cancel"]
    assert _button(rig, card.message_id, "🧪 Confirm dry run")["style"] == "primary"
    await rig.tap(token(rig, card.message_id, "🧪 Confirm dry run"), message=card.message_id)
    final = latest(rig, card.message_id)
    assert final["text"].endswith("🧪 <b>Dry run done — nothing was published</b>")
    assert "reply_markup" not in final


async def test_reviewed_line_counts_opened_views(rig: TgRig) -> None:
    card = await open_card(rig)
    await rig.tap(token(rig, card.message_id, "🧭 Explore"), message=card.message_id)
    await rig.tap(token(rig, card.message_id, "🧾 Files"), message=card.message_id)
    file_view = view_message(rig.ctx, "file")
    await rig.tap(token(rig, file_view, "Next file ▶"), message=file_view)
    await rig.tap(token(rig, card.message_id, "✅ Approve"), message=card.message_id)
    assert "Reviewed: summary ✅ · files 2/2 ✅" in text(rig, card.message_id)


async def test_live_panel_is_red_and_s9_links_the_pr(rig: TgRig) -> None:
    ctx = rig.ctx
    cid, action = packaged_contribution(ctx)
    ctx.control.set_dry_run(False, "uid:1000/operator")
    card = await publish(rig, action)
    await rig.tap(token(rig, card.message_id, "✅ Approve"), message=card.message_id)
    panel = text(rig, card.message_id)
    assert "🔴 <b>LIVE — this opens a public PR</b>" in panel
    assert "Confirm: LIVE — open PR on langfuse/langfuse as test-bot-login" in panel.replace("\u2060", "")
    assert _button(rig, card.message_id, "🚀 Confirm LIVE")["style"] == "danger"
    await rig.tap(token(rig, card.message_id, "🚀 Confirm LIVE"), message=card.message_id)
    final = latest(rig, card.message_id)
    assert final["text"].endswith("🚀 <b>Submitted · PR #77</b>")
    assert rig.buttons(final) == [[{"text": "🔗 Open PR ↗", "url": PR_URL},
                                   {"text": "📋 Copy link", "copy_text": {"text": PR_URL}}]]  # fmt: skip
    assert cid in final["text"]


async def _reasons(rig: TgRig) -> Card:
    card = await open_card(rig)
    await rig.tap(token(rig, card.message_id, "❌ Reject"), message=card.message_id)
    assert labels(rig, card.message_id) == ["Not valuable", "Wrong approach", "Repo unsuitable (7d)", "Other…",
                                            "↩️ Back"]  # fmt: skip
    assert "❌ <b>Reject: why?</b>" in text(rig, card.message_id)
    return card


async def test_reject_back_restores_the_card(rig: TgRig) -> None:
    card = await _reasons(rig)
    await rig.tap(token(rig, card.message_id, "↩️ Back"), message=card.message_id)
    assert status(rig.ctx, card.approval) == "pending"
    assert labels(rig, card.message_id)[0] == "✅ Approve & Submit"
    await rig.tap(token(rig, card.message_id, "✅ Approve"), message=card.message_id)
    assert status(rig.ctx, card.approval) == "armed"


async def test_repo_unsuitable_sets_a_seven_day_cooldown(rig: TgRig) -> None:
    ctx = rig.ctx
    card = await _reasons(rig)
    await rig.tap(token(rig, card.message_id, "Repo unsuitable"), message=card.message_id)
    assert status(ctx, card.approval) == "rejected"
    row = db.one(ctx.conn, "SELECT cooldown_until FROM repo_overrides WHERE upstream = 'langfuse/langfuse'")
    assert row is not None and row[0].startswith("2026-10-24")
    assert "❌ <b>Rejected</b>; nothing was published." in text(rig, card.message_id)


async def test_other_reason_is_asked_and_audited(rig: TgRig) -> None:
    ctx = rig.ctx
    card = await _reasons(rig)
    await rig.tap(token(rig, card.message_id, "Other…"), message=card.message_id)
    assert status(ctx, card.approval) == "rejected"
    await rig.say("Upstream is rewriting this module", reply_to=last_sent_id(rig))
    events = [r[0] for r in ctx.conn.execute("SELECT data_json FROM audit_events WHERE event = 'rejection_reason'")]
    assert len(events) == 1 and "rewriting this module" in events[0]


async def test_reissued_card_after_dry_run_toggle_is_live(rig: TgRig) -> None:
    ctx = rig.ctx
    card = await open_card(rig)
    ctx.reissue_cards(ctx.control.set_dry_run(False, "uid:1000/operator"), "uid:1000/operator")
    await rig.flush()
    new = card_of(ctx, newest_approval(ctx, card.action))
    assert "<b>🔴 LIVE</b>" in text(rig, new.message_id).split("\n")[0]
