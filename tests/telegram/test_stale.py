"""S12 stale approvals (DESIGN §14.11.12; AC 10, BA-11): every invalidation reason has a label, the card
is edited within one housekeeping tick (``recover`` step 3) with a disabled ``⛔ Not approvable`` and the
table's second button, and its verbatim parts become one-line stubs."""

from __future__ import annotations

import ast
import re
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest

from ossbot.common.clock import to_iso
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.review import stale
from tests.conftest import REPO_ROOT
from tests.fakes.holds import hold_request
from tests.telegram.reviewkit import Card, labels, latest, open_card, status, text, toast, token
from tests.telegram.tgharness import TgRig, make_rig

SRC = REPO_ROOT / "src" / "ossbot"
# the positional index of the reason argument per invalidation call
REASON_ARG = {"invalidate_all_open": 0, "invalidate_armed_confirmed": 0, "invalidate_contribution": 1,
              "invalidate_one": 1, "_mark_invalid": 1, "_invalidate": 2, "_invalidate_all": 0}  # fmt: skip
SQL_REASON = re.compile(r"invalidated_reason = '([a-z_:]+)'")


def _literal(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
        return node.left.value  # "live_check:" + ...
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        return str(node.values[0].value)  # f"binding_unavailable:{...}"
    return None


def reason_literals() -> set[str]:
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        gatekeeper = "gatekeeper" in path.relative_to(SRC).parts  # only it writes invalidated_reason
        found |= set(SQL_REASON.findall(source))
        for node in ast.walk(ast.parse(source)):
            if gatekeeper and isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "reason" for t in node.targets):  # fmt: skip
                value = _literal(node.value)  # reason = f"binding_unavailable:{...}" then passed on
                if value is not None and re.fullmatch(r"[a-z_]+:?", value):
                    found.add(value)
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            index = REASON_ARG.get(name)
            if index is not None and len(node.args) > index:
                value = _literal(node.args[index])
                if value is not None:
                    found.add(value)
    return found


def test_every_invalidation_reason_has_a_label() -> None:
    found = reason_literals()
    assert {"reissued", "text_edited", "new_package_rev", "killed", "live_check:", "binding_unavailable:",
            "render_failed", "no_write_token", "changes_requested", "egress_anomaly"} <= found  # fmt: skip
    missing = sorted(r for r in found if r not in stale.REASON_LABELS)
    assert missing == [], missing
    for reason in found:
        label, _ = stale.label_for("invalidated", reason)
        assert not label.startswith("invalidated ("), reason


def test_prefixed_reasons_keep_their_detail() -> None:
    assert stale.label_for("invalidated", "live_check:ci_red") == ("live check failed: ci_red", "retry")
    assert stale.label_for("expired", None)[1] == "retry"
    assert stale.label_for("invalidated", "something_new") == ("invalidated (something_new)", "none")


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


async def _tick(rig: TgRig) -> None:
    rig.frontend.publisher.recover()
    await rig.flush()


def _parts(ctx: AppContext, approval: str) -> list[int]:
    return [int(r[0]) for r in ctx.conn.execute(
        "SELECT message_id FROM tg_views WHERE approval_id = ? AND kind IN ('body_part','commit_msg')",
        (approval,))]  # fmt: skip


async def _expire(rig: TgRig, card: Card) -> None:
    with db.transaction(rig.ctx.conn):
        rig.ctx.conn.execute("UPDATE approvals SET status = 'expired', expires_at = ? WHERE id = ?",
                             (to_iso(rig.ctx.clock.now() - timedelta(minutes=1)), card.approval))  # fmt: skip
    await _tick(rig)


async def test_expired_card_offers_retry_and_stubs_its_parts(rig: TgRig) -> None:
    ctx = rig.ctx
    card = await open_card(rig)
    await _expire(rig, card)
    shown = text(rig, card.message_id)
    assert shown.split("\n")[0].endswith("⌛ <b>Expired</b> · approval was valid 72 h")
    assert "<s>" in shown
    assert labels(rig, card.message_id) == ["⛔ Not approvable", "🔁 Retry"]
    assert rig.buttons(latest(rig, card.message_id))[0][0]["disabled"] == {}
    for part in _parts(ctx, card.approval):
        assert text(rig, part) == f"⚪ part of superseded {card.cid} r1"
    await rig.tap(token(rig, card.message_id, "🔁 Retry"), message=card.message_id)
    assert toast(rig) == "🔁 A fresh card follows."


async def test_the_sweep_runs_once_per_approval(rig: TgRig) -> None:
    card = await open_card(rig)
    await _expire(rig, card)
    edits = len([m for m, _ in rig.req.calls if m == "editMessageText"])
    await _tick(rig)
    assert len([m for m, _ in rig.req.calls if m == "editMessageText"]) == edits


async def test_reissued_card_offers_show_current(rig: TgRig) -> None:
    ctx = rig.ctx
    card = await open_card(rig)
    ctx.reissue(card.cid, "test", keep_expiry=True)
    await _tick(rig)
    assert status(ctx, card.approval) == "invalidated"
    assert "⚪ <b>Invalidated</b> · moved to a newer message" in text(rig, card.message_id)
    assert labels(rig, card.message_id) == ["⛔ Not approvable", "⬇️ Show current"]
    await rig.tap(token(rig, card.message_id, "⬇️ Show current"), message=card.message_id)
    assert toast(rig) == "Re-sent the card"


async def test_no_show_current_without_an_open_card(rig: TgRig) -> None:
    ctx = rig.ctx
    card = await open_card(rig)
    with db.transaction(ctx.conn):
        ctx.approvals.invalidate_contribution(card.cid, "killed", "system")
    await _tick(rig)
    assert "⚪ <b>Invalidated</b> · kill" in text(rig, card.message_id)
    assert labels(rig, card.message_id) == ["⛔ Not approvable"]


async def test_held_card_offers_no_second_button(rig: TgRig) -> None:
    """Open-discovery §13.3: a hold invalidates the cid's card; no Retry/Show current while held."""
    ctx = rig.ctx
    card = await open_card(rig)
    ctx.holds.hold(card.cid, hold_request(), "uid:2002/sandbox")
    await _tick(rig)
    assert status(ctx, card.approval) == "invalidated"
    assert "⚪ <b>Invalidated</b> · egress anomaly: contribution held" in text(rig, card.message_id)
    assert labels(rig, card.message_id) == ["⛔ Not approvable"]


async def test_newer_revision_header(rig: TgRig) -> None:
    ctx = rig.ctx
    card = await open_card(rig)
    with db.transaction(ctx.conn):
        ctx.approvals.invalidate_contribution(card.cid, "new_package_rev", "system")
    await _tick(rig)
    assert "⚪ <b>Superseded</b> · r2 is newer" in text(rig, card.message_id)
