"""``/fix <cid|PR url> <notes>`` (ADR-0003 addendum 2026-10-23; DESIGN §14.14.1): a one-tap confirm card,
then the owner fix round (one change request, WATCHING -> IMPLEMENTING by the human). Goldens of the
confirm (classic only, U3) and of the started note (classic and rich); every refusal leaves no change
request and no state change; the tokens are single use, message-bound and carry no approval id."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest

from ossbot.common.schemas import State
from ossbot.gatekeeper import db, owner_fix
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.commands import fix
from ossbot.gatekeeper.telegram.commands.registry import COMMANDS
from ossbot.gatekeeper.telegram.ui import blocks, classic, rich
from tests.conftest import insert_contribution, write_flag
from tests.telegram import golden
from tests.telegram.reviewkit import labels, sent_with, text, token
from tests.telegram.tgharness import TgRig, make_rig

NOTES = "make the storage listener ignore sessionStorage"
URL = "https://github.com/o/r/pull/5678"
CARD = "watch rounds are not used"  # a line of the confirm card


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def watching(ctx: AppContext, state: State = State.WATCHING, **cols: Any) -> str:
    cols.setdefault("pr_number", 5678)
    cols.setdefault("pr_url", URL)
    return insert_contribution(ctx, state, upstream="o/r", **cols)


def requests(ctx: AppContext, cid: str) -> list[Any]:
    return list(ctx.conn.execute("SELECT * FROM change_requests WHERE cid = ?", (cid,)).fetchall())


def contribution(ctx: AppContext, cid: str) -> Any:
    row = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
    assert row is not None
    return row


def keyboard(view: Any) -> str:
    return "\n".join(" | ".join(r) for r in view.keyboard_labels())


def test_command_is_registered_as_request_io() -> None:
    spec = COMMANDS["fix"]
    assert (spec.safety, spec.effect) == ("request", "io")
    assert len(spec.help) <= 60 and spec.help.startswith("Fix")


def test_confirm_and_started_goldens(ctx: AppContext) -> None:
    cid = watching(ctx, owner_fix_rounds=1)
    view = fix.confirm_view(ctx, contribution(ctx, cid), NOTES + " <b>&amp;</b>")
    assert view.rich is False  # a confirm is never rich (U3)
    shown = classic.render(view).text
    assert f"<code>{cid}</code>" in shown and "&lt;b&gt;" in shown  # the notes are escaped
    golden.check("fix_confirm.html", shown.replace(cid, "c_CID") + "\n--\n" + keyboard(view) + "\n")
    started = fix.started_view(owner_fix.RoundStarted(cid, 2, 5, "r_x", 5678))
    golden.check("fix_started.html", classic.render(started).text.replace(cid, "c_CID") + "\n")
    doc = blocks.dump(rich.render(replace(started, rich=True)))
    dumped = json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True)
    golden.check("fix_started.json", dumped.replace(cid, "c_CID") + "\n")


def test_long_notes_are_cut_on_the_card_only(ctx: AppContext) -> None:
    cid = watching(ctx)
    view = fix.confirm_view(ctx, contribution(ctx, cid), "y" * 4000)
    assert "y" * 301 not in classic.render(view).text
    btn = view.rows[0][0]
    assert btn.purpose == "pr_fix_confirm" and btn.arg is not None and len(btn.arg["notes"]) == 4000


async def test_fix_confirm_starts_the_round_once(rig: TgRig) -> None:
    ctx = rig.ctx
    cid = watching(ctx)
    await rig.say(f"/fix {cid} {NOTES}")
    mid = sent_with(rig, CARD)
    assert labels(rig, mid) == ["🔧 Start fix round", "↩️ Cancel"]
    assert requests(ctx, cid) == [] and contribution(ctx, cid)["state"] == "WATCHING"  # nothing before the tap
    tokens = ctx.conn.execute("SELECT purpose, approval_id, tg_message_id, single_use, arg FROM ui_tokens"
                              " WHERE cid = ? ORDER BY purpose", (cid,)).fetchall()  # fmt: skip
    assert [(t["purpose"], t["approval_id"], t["tg_message_id"], t["single_use"]) for t in tokens] == [
        ("pr_fix_cancel", None, mid, 1), ("pr_fix_confirm", None, mid, 1)]  # fmt: skip
    assert json.loads(tokens[1]["arg"]) == {"cid": cid, "notes": NOTES}
    confirm = token(rig, mid, "🔧 Start fix round")
    await rig.tap(confirm, message=mid)
    c = contribution(ctx, cid)
    assert (c["state"], c["state_reason"], c["owner_fix_rounds"], c["watch_rounds"]) == (
        "IMPLEMENTING", "owner_fix_round", 1, 0)  # fmt: skip
    rows = requests(ctx, cid)
    assert [(r["source"], r["text"]) for r in rows] == [("owner", NOTES)]
    assert "Fix round 1/5 started" in text(rig, mid) and labels(rig, mid) == []
    await rig.tap(confirm, message=mid)  # single use: a replay does nothing
    assert len(requests(ctx, cid)) == 1 and contribution(ctx, cid)["owner_fix_rounds"] == 1


async def test_fix_by_pr_url(rig: TgRig) -> None:
    cid = watching(rig.ctx)
    await rig.say(f"/fix {URL} {NOTES}")
    mid = sent_with(rig, CARD)
    assert cid in text(rig, mid)


async def test_cancel_kills_the_confirm(rig: TgRig) -> None:
    ctx = rig.ctx
    cid = watching(ctx)
    await rig.say(f"/fix {cid} {NOTES}")
    mid = sent_with(rig, CARD)
    confirm = token(rig, mid, "🔧 Start fix round")
    await rig.tap(token(rig, mid, "↩️ Cancel"), message=mid)
    assert "Not started." in text(rig, mid)
    await rig.tap(confirm, message=mid)  # the edit retired the card's tokens
    assert requests(ctx, cid) == [] and contribution(ctx, cid)["state"] == "WATCHING"


@pytest.mark.parametrize(
    ("setup", "args", "needle"),
    [
        (None, "", "Usage: /fix"),
        ("watching", "{cid}", "Usage: /fix"),
        (None, "c_0000000000 notes", "no such contribution"),
        (None, "https://github.com/o/r/pull/1 notes", "no ossbot PR at that URL"),
        (None, "https://github.com/o/r/issues/1 notes", "Usage: /fix"),
        ("awaiting", "{cid} notes", "use ✏️ Request changes on its card"),
        ("submitted", "{cid} notes", "state SUBMITTED"),
        ("capped", "{cid} notes", "all 5 owner fix rounds are used"),
        ("disabled", "{cid} notes", "commits not made by ossbot; fixes are disabled for PR"),
        ("paused", "{cid} notes", "paused or killed; /resume first"),
    ],
)
async def test_refusals(rig: TgRig, setup: str | None, args: str, needle: str) -> None:
    ctx = rig.ctx
    cid = ""
    if setup in {"watching", "paused"}:
        cid = watching(ctx)
    elif setup == "awaiting":
        cid = watching(ctx, State.AWAITING_APPROVAL)
    elif setup == "submitted":
        cid = watching(ctx, State.SUBMITTED)
    elif setup == "capped":
        cid = watching(ctx, owner_fix_rounds=5)
    elif setup == "disabled":
        cid = watching(ctx, fixes_disabled_reason="branch_moved")
    if setup == "paused":
        write_flag(ctx.paths.paused_flag, {"reason": "x"})
    before = [tuple(r) for r in ctx.conn.execute("SELECT id, state FROM contributions ORDER BY id")]
    await rig.say(f"/fix {args.format(cid=cid)}".rstrip())
    assert needle in rig.last_text()
    assert "Start fix round" not in json.dumps(rig.req.calls, ensure_ascii=False)
    assert ctx.conn.execute("SELECT COUNT(*) FROM change_requests").fetchone()[0] == 0
    assert [tuple(r) for r in ctx.conn.execute("SELECT id, state FROM contributions ORDER BY id")] == before


async def test_paused_between_the_card_and_the_tap(rig: TgRig) -> None:
    ctx = rig.ctx
    cid = watching(ctx)
    await rig.say(f"/fix {cid} {NOTES}")
    mid = sent_with(rig, CARD)
    write_flag(ctx.paths.paused_flag, {"reason": "x"})
    await rig.tap(token(rig, mid, "🔧 Start fix round"), message=mid)
    assert "Not started: paused or killed; /resume first." in text(rig, mid)
    assert requests(ctx, cid) == [] and contribution(ctx, cid)["state"] == "WATCHING"


async def test_capped_between_the_card_and_the_tap(rig: TgRig) -> None:
    ctx = rig.ctx
    cid = watching(ctx, owner_fix_rounds=4)
    await rig.say(f"/fix {cid} {NOTES}")
    mid = sent_with(rig, CARD)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE contributions SET owner_fix_rounds = 5 WHERE id = ?", (cid,))
    await rig.tap(token(rig, mid, "🔧 Start fix round"), message=mid)
    assert "all 5 owner fix rounds are used" in text(rig, mid)
    assert requests(ctx, cid) == [] and contribution(ctx, cid)["state"] == "WATCHING"
