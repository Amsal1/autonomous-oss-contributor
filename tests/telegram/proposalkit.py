"""Shared helpers for the gate-1 presentation tests (PLAN item 46): a proposal stored through
``ProposalService.submit`` and its card delivered over the fake Bot API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from tests.telegram.tgharness import TgRig
from tests.unit.gatekeeper.test_proposals import scouting, submit


@dataclass(frozen=True)
class PCard:
    cid: str
    version: int
    approval: str
    message_id: int


def card_of(ctx: AppContext, approval: str) -> PCard:
    row = db.one(ctx.conn, "SELECT cid, version, tg_message_id FROM proposal_approvals WHERE id = ?", (approval,))
    assert row is not None and row["tg_message_id"] is not None, approval
    return PCard(str(row["cid"]), int(row["version"]), approval, int(row["tg_message_id"]))


async def proposal_card(rig: TgRig, *, source: str = "discover", vetting: dict[str, Any] | None = None,
                        notes: str | None = None, issue_title: str = "Fix trace export when spans are empty",
                        **over: Any) -> PCard:  # fmt: skip
    """A scouting contribution, its v1 proposal stored and the card delivered."""
    cols: dict[str, Any] = {"issue_title": issue_title,
                            "issue_url": "https://github.com/langfuse/langfuse/issues/1234"}  # fmt: skip
    if vetting is not None:
        cols["vetting_json"] = json.dumps(vetting)
    if notes is not None:
        cols["notes"] = notes
    cid = scouting(rig.ctx, source=source, **cols)
    out = submit(rig.ctx, cid, **over)
    await rig.flush()
    return card_of(rig.ctx, str(out["approval_id"]))


async def revise(rig: TgRig, cid: str, revises: int, **over: Any) -> PCard:
    """The next version (scout's answer to the feedback), delivered."""
    out = submit(rig.ctx, cid, revises=revises, **over)
    await tick(rig)
    return card_of(rig.ctx, str(out["approval_id"]))


async def tick(rig: TgRig) -> None:
    """One housekeeping tick of the card publisher (S12 of ended approvals), then deliver."""
    await rig.flush()
    rig.frontend.publisher.recover()
    await rig.flush()


def state(ctx: AppContext, cid: str) -> str:
    return str(db.one(ctx.conn, "SELECT state FROM contributions WHERE id = ?", (cid,))[0])  # type: ignore[index]


def approval_status(ctx: AppContext, approval: str) -> tuple[str, str | None]:
    row = db.one(ctx.conn, "SELECT status, rejected_reason FROM proposal_approvals WHERE id = ?", (approval,))
    assert row is not None
    return str(row["status"]), row["rejected_reason"]
