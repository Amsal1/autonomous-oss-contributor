"""🌐 Rerun with network on Telegram (ADR-0024, design §4d/§4f): the FYI button opens a confirm card (classic
only, like /fix); only its 🌐 Rerun now tap drives the human edge; refusals are toasts with the §4d lines;
cancel retires both buttons; a replay, a foreign user and another message are refused."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

import pytest

from ossbot.common.schemas import State
from ossbot.gatekeeper import db, network_rerun
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.state_machine import Actor
from ossbot.gatekeeper.telegram.commands import network_rerun as cmd
from ossbot.gatekeeper.telegram.ui import classic
from tests.conftest import consumed_proposal, insert_contribution, write_flag
from tests.telegram import golden
from tests.telegram.reviewkit import labels, sent_with, text, toast, token
from tests.telegram.tgharness import TgRig, make_rig
from tests.unit.gatekeeper.test_state_machine import GUARD_ORDER, _guard_case

S = State


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def _switches(ctx: AppContext, mode: str = "sandbox_open", setting: str = "on_request") -> None:
    default = ctx.config.egress.mode_default
    if ctx.control.egress_mode(default) != mode:
        ctx.control.set_egress_mode(mode, "tg:4242", default, egress=ctx.config.egress)  # type: ignore[arg-type]
    ctx.control.set_implement_network(setting, "tg:4242", default)  # type: ignore[arg-type]


def _failed(ctx: AppContext, issue: int = 21375) -> str:
    """kestra-io/kestra is not a config target here: runatlantis/atlantis stands in for the real repo."""
    cid = insert_contribution(ctx, S.IMPLEMENTING, upstream="runatlantis/atlantis", issue_number=issue,
                              proposal_version=1)  # fmt: skip
    consumed_proposal(ctx, cid)
    ctx.state_machine.transition(cid, S.IMPLEMENTING, S.FAILED, Actor.WORKER,
                                 {"error_class": "needs_network", "summary": "deps"})  # fmt: skip
    return cid


def _state(ctx: AppContext, cid: str) -> Any:
    return db.one(ctx.conn, "SELECT state, network_reruns FROM contributions WHERE id = ?", (cid,))


async def _confirm_card(rig: TgRig, cid: str) -> int:
    await rig.flush()
    fyi = sent_with(rig, "Failed")
    await rig.tap(token(rig, fyi, "🌐 Rerun with network"), message=fyi)
    await rig.flush()
    return sent_with(rig, "Rerun with network?")


def test_confirm_card_golden(ctx: AppContext) -> None:
    _switches(ctx)
    cid = _failed(ctx)
    view = cmd.confirm_view(ctx, db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,)))
    keyboard = "\n".join(" | ".join(row) for row in view.keyboard_labels())
    golden.check("network_rerun_confirm.html", classic.render(view).text.replace(cid, "c_CID") + "\n--\n"
                 + keyboard + "\n")  # fmt: skip
    assert not view.rich and view.kind == "confirm_prompt"
    assert "(now DRY RUN)" in classic.render(view).text


async def test_button_then_confirm_starts_the_rerun(rig: TgRig) -> None:
    ctx = rig.ctx
    _switches(ctx)
    cid = _failed(ctx)
    mid = await _confirm_card(rig, cid)
    assert labels(rig, mid) == ["🌐 Rerun now", "↩️ Cancel"]
    assert tuple(_state(ctx, cid)) == ("FAILED", 0)  # nothing before the confirm tap
    await rig.tap(token(rig, mid, "🌐 Rerun now"), message=mid)
    assert toast(rig) == "🌐 Rerun started."
    assert tuple(_state(ctx, cid)) == ("PROPOSAL_APPROVED", 1)
    body = text(rig, mid)
    assert "Rerun 1/1 started" in body and "fetch runs now" in body and "nothing is pushed" in body
    assert labels(rig, mid) == []
    event = db.one(ctx.conn, "SELECT actor FROM audit_events WHERE event = 'transition' AND cid = ? ORDER BY seq"
                   " DESC LIMIT 1", (cid,))  # fmt: skip
    assert event["actor"] == "tg:4242"


async def test_started_while_draining_says_when(rig: TgRig) -> None:
    ctx = rig.ctx
    _switches(ctx)
    cid = _failed(ctx)
    mid = await _confirm_card(rig, cid)
    ctx.control.drain("tg:4242", "test", 60, "workers")
    await rig.tap(token(rig, mid, "🌐 Rerun now"), message=mid)
    assert "starts when the bot is up" in text(rig, mid)


async def test_cancel_retires_both_and_a_replay_is_refused(rig: TgRig) -> None:
    ctx = rig.ctx
    _switches(ctx)
    cid = _failed(ctx)
    mid = await _confirm_card(rig, cid)
    rerun = token(rig, mid, "🌐 Rerun now")
    await rig.tap(token(rig, mid, "↩️ Cancel"), message=mid)
    assert "Not started." in text(rig, mid)
    await rig.tap(rerun, message=mid)  # the other button died with the cancel
    assert tuple(_state(ctx, cid)) == ("FAILED", 0)


async def test_double_tap_is_refused(rig: TgRig) -> None:
    ctx = rig.ctx
    _switches(ctx)
    cid = _failed(ctx)
    mid = await _confirm_card(rig, cid)
    rerun = token(rig, mid, "🌐 Rerun now")
    await rig.tap(rerun, message=mid)
    await rig.tap(rerun, message=mid)  # consumed: no second edge
    assert tuple(_state(ctx, cid)) == ("PROPOSAL_APPROVED", 1)


async def test_foreign_user_chat_and_message_are_refused(rig: TgRig) -> None:
    ctx = rig.ctx
    _switches(ctx)
    cid = _failed(ctx)
    mid = await _confirm_card(rig, cid)
    rerun = token(rig, mid, "🌐 Rerun now")
    await rig.tap(rerun, message=mid, user=999, chat=999)
    await rig.tap(rerun, message=mid, chat=999)
    await rig.tap(rerun, message=mid + 1)
    assert tuple(_state(ctx, cid)) == ("FAILED", 0) and labels(rig, mid) == ["🌐 Rerun now", "↩️ Cancel"]


async def test_the_confirm_rechecks_the_switches(rig: TgRig) -> None:
    """A pending confirm card is not invalidated by a setting change; the tap re-checks it (design §4c)."""
    ctx = rig.ctx
    _switches(ctx)
    cid = _failed(ctx)
    mid = await _confirm_card(rig, cid)
    ctx.control.set_implement_network("off", "tg:4242", ctx.config.egress.mode_default)
    await rig.tap(token(rig, mid, "🌐 Rerun now"), message=mid)
    assert "Not started: implement network is off; set on_request or auto in /egress." in text(rig, mid)
    assert tuple(_state(ctx, cid)) == ("FAILED", 0)


@pytest.mark.parametrize("code", [*GUARD_ORDER, "not_found", "not_failed", "stopped"])
def test_one_refusal_line_per_code(ctx: AppContext, code: str) -> None:
    if code == "not_found":
        cid = "c_0000000000"
    elif code == "not_failed":
        _switches(ctx)
        cid = insert_contribution(ctx, S.IMPLEMENTING, upstream="runatlantis/atlantis")
    elif code == "stopped":
        _switches(ctx)
        cid = _failed(ctx)
        write_flag(ctx.paths.paused_flag, {"reason": "x"})
    else:
        cid = _guard_case(ctx, code)
    assert network_rerun.refusal(ctx, cid) == code
    line = cmd.refusal_text(ctx, code, cid)
    assert line and line != code
    # the card edit (the toast is plain text): any cid in the line is one tap to copy
    shown = classic.render(cmd._note(cmd._not_started(ctx, code, cid))).text
    golden.check(f"network_rerun_refusal_{code}.html", re.sub(r"c_[0-9A-Z]{10}", "c_CID", shown) + "\n")


def test_rerun_reason_wording_is_c_d21(ctx: AppContext) -> None:
    cid = _guard_case(ctx, "rerun_reason")
    assert cmd.refusal_text(ctx, "rerun_reason", cid) == (
        "it failed for implement_attempts, which isn't a network or sandbox-gap failure"
    )


async def test_refusal_is_a_toast_on_the_button(rig: TgRig) -> None:
    ctx = rig.ctx
    _switches(ctx, mode="open")
    cid = _failed(ctx)
    await rig.flush()
    fyi = sent_with(rig, "Failed")
    await rig.tap(token(rig, fyi, "🌐 Rerun with network"), message=fyi)
    assert toast(rig) == "Not started: the egress mode is open; rerun with network needs sandbox_open (/egress)."
    assert tuple(_state(ctx, cid)) == ("FAILED", 0)
