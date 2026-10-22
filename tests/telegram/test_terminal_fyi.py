"""The terminal FYI after gate 1 (DESIGN §14.13.1, §9.10): classic and rich goldens, the cid one tap to copy,
untrusted summary text redacted and escaped and cut, no buttons, P2 held in quiet hours, delivered through
the rig with the copyable-id lint."""

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
from ossbot.gatekeeper.state_machine import Actor
from ossbot.gatekeeper.telegram import terminal_fyi
from ossbot.gatekeeper.telegram.notify import Notifier
from ossbot.gatekeeper.telegram.ui import blocks, classic, rich
from ossbot.gatekeeper.telegram.ui import kit as k
from tests.conftest import consumed_proposal, insert_contribution
from tests.telegram import golden
from tests.telegram.reviewkit import labels, sent_with, text
from tests.telegram.tgharness import TgRig, make_rig

GHP = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # a token-shaped string a hostile repo could plant
SUMMARY = ("The stale-apply guards already landed on main (server/events/db_updater.go); the bug no longer"
           " reproduces. Suggested next step: comment on #2607.")  # fmt: skip
S = State


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def _after_gate1(ctx: AppContext, state: State, **cols: object) -> str:
    cid = insert_contribution(ctx, state, upstream="runatlantis/atlantis", issue_number=2607, proposal_version=1,
                              **cols)  # fmt: skip
    consumed_proposal(ctx, cid)
    return cid


def _goldens(name: str, view: k.View, cid: str) -> None:
    golden.check(f"{name}.html", classic.render(view).text.replace(cid, "c_CID") + "\n")
    doc = blocks.dump(rich.render(replace(view, rich=True)))
    dumped = json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True)
    golden.check(f"{name}.json", dumped.replace(cid, "c_CID") + "\n")


def test_abandoned_with_summary_classic_and_rich() -> None:
    cid = "c_D1B6RE1ZRS"
    view = terminal_fyi.terminal_view(cid, "runatlantis/atlantis#2607", S.PLANNING, S.ABANDONED, "plan_infeasible",
                                      SUMMARY)  # fmt: skip
    _goldens("terminal_fyi_abandoned", view, cid)
    shown = classic.render(view).text
    assert f"<code>{cid}</code>" in shown and "⚪" in shown and "Stage: planning" in shown
    assert view.rows == [] and view.kind == "fyi"


def test_failed_without_summary_classic_and_rich() -> None:
    cid = "c_D1CB6PG35G"
    view = terminal_fyi.terminal_view(cid, "SigNoz/signoz", S.PLANNING, S.FAILED, "output_invalid", "")
    _goldens("terminal_fyi_failed", view, cid)
    shown = classic.render(view).text
    assert f"<code>{cid}</code>" in shown and "❌" in shown and "Why:" not in shown


def test_untrusted_summary_is_redacted_escaped_and_cut() -> None:
    cid = "c_D1B6RE1ZRS"
    summary = f"leaked {GHP} <b>bold</b> " + "x" * 900
    view = terminal_fyi.terminal_view(cid, "a/b#1", S.PLANNING, S.ABANDONED, "plan_infeasible", summary)
    shown = classic.render(view).text
    assert GHP not in shown and "[REDACTED]" in shown
    assert "<b>bold" not in shown and "&lt;b&gt;bold&lt;/" in shown  # escaped (and made visible)
    assert "x" * 600 not in shown and "…" in shown
    dumped = json.dumps(blocks.dump(rich.render(replace(view, rich=True))))
    assert GHP not in dumped and "[REDACTED]" in dumped


async def test_p2_fyi_is_held_in_quiet_hours(ctx: AppContext) -> None:
    notifier = Notifier(ctx)
    ctx.outbox.hold_policy = notifier.decide
    notifier.prefs.set("quiet_until", to_iso(ctx.clock.now() + timedelta(hours=1)))
    cid = _after_gate1(ctx, S.PLANNING)
    ctx.state_machine.transition(cid, S.PLANNING, S.ABANDONED, Actor.WORKER, {"reason": "plan_infeasible"})
    row = db.one(ctx.conn, "SELECT priority, status, thread_key FROM tg_outbox WHERE dedupe_key = ?",
                 (f"terminal:{cid}",))  # fmt: skip
    assert row is not None and tuple(row) == (2, "held", "feed")
    assert ctx.outbox.due() == []  # not sent before the quiet window ends


async def test_delivered_through_the_rig(rig: TgRig) -> None:
    ctx = rig.ctx
    cid = _after_gate1(ctx, S.PLANNING)
    ctx.state_machine.transition(cid, S.PLANNING, S.ABANDONED, Actor.WORKER,
                                 {"reason": "plan_infeasible", "summary": SUMMARY})  # fmt: skip
    await rig.flush()  # the copyable-id lint (idcheck) runs on every payload here
    mid = sent_with(rig, "Abandoned")
    assert f"<code>{cid}</code>" in text(rig, mid) and "db_updater.go" in text(rig, mid)
    assert labels(rig, mid) == []
