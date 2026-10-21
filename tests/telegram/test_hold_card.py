"""The P1 egress-anomaly hold card and its Resume two-tap (open-discovery design §13.3): classic and rich
goldens, hostile host names redacted and escaped, Resume confirm and release, stale and foreign taps
refused, priority 1 and not held in quiet hours."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta

import pytest

from ossbot.common.clock import to_iso
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import hold_cards
from ossbot.gatekeeper.telegram.notify import Notifier
from ossbot.gatekeeper.telegram.ui import blocks, classic, rich
from tests.conftest import insert_contribution
from tests.fakes.holds import hold_request
from tests.telegram import golden
from tests.telegram.reviewkit import labels, sent_with, text, toast, token
from tests.telegram.tgharness import TgRig, make_rig

SANDBOX = "uid:2002/sandbox"
GHP = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # a token-shaped "host" a hostile repo could log


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def _held(ctx: AppContext, **overrides: object) -> str:
    cid = insert_contribution(ctx, State.VALIDATING, upstream="acme/widgets", issue_number=123)
    ctx.holds.hold(cid, hold_request(**overrides), SANDBOX)
    return cid


def test_card_classic_and_rich(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.VALIDATING, upstream="acme/widgets", issue_number=123)
    ctx.holds.publish_hold = lambda v: None
    found, _ = ctx.holds.hold(cid, hold_request(), SANDBOX)
    view = hold_cards.hold_view(ctx, found)
    keyboard = "\n".join(" | ".join(row) for row in view.keyboard_labels())
    golden.check("hold_card.html", classic.render(view).text + "\n--\n" + keyboard + "\n")
    doc = blocks.dump(rich.render(replace(view, rich=True)))
    golden.check("hold_card.json", json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    confirm = hold_cards.confirm_view(ctx, found)
    golden.check("hold_card_confirm.html", classic.render(confirm).text + "\n--\n"
                 + "\n".join(" | ".join(r) for r in confirm.keyboard_labels()) + "\n")  # fmt: skip


def test_hostile_host_names_are_escaped_and_redacted(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.VALIDATING, upstream="acme/widgets", issue_number=7)
    ctx.holds.publish_hold = lambda v: None
    found, _ = ctx.holds.hold(cid, hold_request(), SANDBOX)
    # past the schema (which refuses both), so the renderers' own redaction and escaping are what's tested
    hosts = [f"{GHP}.evil.example", "a<b>.example", *[f"h{i}.example" for i in range(8)]]
    found = found.model_copy(update={"detail": found.detail.model_copy(update={"top_hosts": hosts})})
    view = hold_cards.hold_view(ctx, found)
    shown = classic.render(view).text
    assert GHP not in shown and "[REDACTED]" in shown
    assert GHP not in json.dumps(blocks.dump(rich.render(replace(view, rich=True))))
    assert "<b>.example" not in shown and "a&lt;b&gt;.example" in shown
    assert "h3.example" not in shown  # at most five hosts


async def test_card_is_p1_in_control_and_not_held_in_quiet_hours(ctx: AppContext) -> None:
    notifier = Notifier(ctx)
    ctx.outbox.hold_policy = notifier.decide
    notifier.prefs.set("quiet_until", to_iso(ctx.clock.now() + timedelta(hours=1)))
    _held(ctx)
    row = db.one(ctx.conn, "SELECT priority, status, thread_key, payload_json FROM tg_outbox ORDER BY id DESC LIMIT 1")
    assert (row["priority"], row["status"], row["thread_key"]) == (1, "pending", "control")
    assert "Egress anomaly" in row["payload_json"]
    assert [r.id for r in ctx.outbox.due()]  # due now, not held until the quiet window ends


async def test_resume_two_tap_releases_the_hold(rig: TgRig) -> None:
    ctx = rig.ctx
    cid = _held(ctx)
    await rig.flush()
    mid = sent_with(rig, "Egress anomaly")
    assert labels(rig, mid) == ["▶️ Resume…"]
    await rig.tap(token(rig, mid, "▶️ Resume…"), message=mid)
    assert "Resume acme/widgets#123?" in text(rig, mid).replace("<b>", "").replace("</b>", "")
    assert "ossctl targets strict acme/widgets" in text(rig, mid)
    assert labels(rig, mid) == ["▶️ Resume", "↩️ Cancel"]
    assert ctx.holds.is_held(cid)
    await rig.tap(token(rig, mid, "▶️ Resume"), message=mid)
    assert toast(rig) == "▶️ Resumed."
    assert not ctx.holds.is_held(cid)
    assert "Resumed" in text(rig, mid) and labels(rig, mid) == []
    released = db.one(ctx.conn, "SELECT released_by FROM contribution_holds WHERE cid = ?", (cid,))
    assert released["released_by"] == "tg:4242"
    assert db.one(ctx.conn, "SELECT 1 FROM audit_events WHERE event = 'contribution_hold_released' AND cid = ?",
                  (cid,)) is not None  # fmt: skip


async def test_cancel_restores_the_card(rig: TgRig) -> None:
    cid = _held(rig.ctx)
    await rig.flush()
    mid = sent_with(rig, "Egress anomaly")
    await rig.tap(token(rig, mid, "▶️ Resume…"), message=mid)
    await rig.tap(token(rig, mid, "↩️ Cancel"), message=mid)
    assert "Egress anomaly" in text(rig, mid) and labels(rig, mid) == ["▶️ Resume…"]
    assert rig.ctx.holds.is_held(cid)


async def test_foreign_and_stale_taps_are_refused(rig: TgRig) -> None:
    ctx = rig.ctx
    cid = _held(ctx)
    await rig.flush()
    mid = sent_with(rig, "Egress anomaly")
    resume = token(rig, mid, "▶️ Resume…")
    await rig.tap(resume, message=mid, user=999, chat=999)  # not the owner
    assert labels(rig, mid) == ["▶️ Resume…"] and ctx.holds.is_held(cid)
    await rig.tap(resume, message=mid + 1)  # another message
    assert labels(rig, mid) == ["▶️ Resume…"] and ctx.holds.is_held(cid)
    await rig.tap(resume, message=mid)
    confirm = token(rig, mid, "▶️ Resume")
    ctx.holds.release(cid, "uid:1000/operator")  # ossctl egress release meanwhile
    await rig.tap(confirm, message=mid)
    assert toast(rig) == hold_cards.GONE
    assert labels(rig, mid) == []
    _, created = ctx.holds.hold(cid, hold_request(run_id="validate-two"), SANDBOX)  # a new hold, a new card
    assert created
    await rig.tap(resume, message=mid)  # the first card's token names the released hold
    assert ctx.holds.is_held(cid)


async def test_confirm_is_single_use(rig: TgRig) -> None:
    cid = _held(rig.ctx)
    await rig.flush()
    mid = sent_with(rig, "Egress anomaly")
    await rig.tap(token(rig, mid, "▶️ Resume…"), message=mid)
    confirm = token(rig, mid, "▶️ Resume")
    await rig.tap(confirm, message=mid)
    rig.ctx.holds.hold(cid, hold_request(run_id="validate-two"), SANDBOX)
    await rig.tap(confirm, message=mid)  # replayed: the token was consumed
    assert rig.ctx.holds.is_held(cid)
