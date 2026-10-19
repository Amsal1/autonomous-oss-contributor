"""Review commands (DESIGN §14.14.1): ``/queue`` and ``/approve [cid]``.

``/approve <cid>`` never approves anything: it re-sends the contribution's open cards through
``AppContext.reissue(cid, actor, keep_expiry=True)`` (§14.18.4 deltas 5 and 7), with the eligibility
answers of §14.18.4.3. Without a cid it shows the queue.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ossbot.common.ids import ID_RE
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalError
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.actions import Tap, action
from ossbot.gatekeeper.telegram.commands.registry import CommandContext, Reply, command
from ossbot.gatekeeper.telegram.escape import esc
from ossbot.gatekeeper.telegram.review import queue, resolve
from ossbot.gatekeeper.telegram.review.resolve import SKILL_PATTERN
from ossbot.gatekeeper.telegram.ui import send as ui_send


@command("queue", help="What waits for you, gate 2 first, with Open buttons", safety="read")
async def queue_cmd(ctx: CommandContext, args: list[str]) -> Reply:
    return Reply("", view=queue.view(ctx.app))


def resend(app: AppContext, cid: str, actor: str) -> str:
    try:
        ids = app.reissue(cid, actor, keep_expiry=True)
    except ApprovalError as exc:
        if exc.code == "submitting":
            return "Submitting… try again in a moment"
        if exc.code == "unknown_contribution":
            return f"Unknown contribution {cid}"
        proposal = db.one(app.conn, "SELECT 1 FROM proposal_approvals WHERE cid = ? AND status IN ('pending','armed')",
                          (cid,))  # fmt: skip
        if proposal is not None:
            try:
                app.reissue_proposal(cid, actor, keep_expiry=True)
            except ApprovalError:
                return resolve.nothing_text(resolve.state_of(app.conn, cid))
            return "Re-sent the proposal card"
        return resolve.nothing_text(resolve.state_of(app.conn, cid))
    return "Re-sent the card" if len(ids) == 1 else f"Re-sent {len(ids)} cards"


@command("approve", help="Re-send a contribution's card (never approves by itself)", safety="read")
async def approve_cmd(ctx: CommandContext, args: list[str]) -> Reply:
    if not args:
        return Reply("", view=queue.view(ctx.app))
    cid = args[0].strip()
    if not ID_RE.match(cid) or not cid.startswith("c_"):
        return Reply("Usage: /approve c_0123456789 (re-sends its card; you approve on the card)")
    return Reply(esc(resend(ctx.app, cid, ctx.actor)))


class OpenArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    family: Literal["a", "p", "s"]
    subject: str = Field(min_length=1, max_length=64)


class SortArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sort: Literal["priority", "oldest"]
    page: int = Field(default=1, ge=1, le=1000)


@action("queue_open", arg=OpenArg)
async def queue_open(tap: Tap) -> str:
    a: OpenArg = tap.arg
    if a.family == "s":
        if not re.match(SKILL_PATTERN, a.subject):
            return resolve.SUPERSEDED
        try:
            tap.app.reissue_skill(a.subject, tap.actor, keep_expiry=True)
        except ApprovalError:
            return f"No open card for skill {a.subject}"
        return "Re-sent the skill card"
    if not ID_RE.match(a.subject):
        return resolve.SUPERSEDED
    if a.family == "p":
        try:
            tap.app.reissue_proposal(a.subject, tap.actor, keep_expiry=True)
        except ApprovalError as exc:
            return "Submitting… try again in a moment" if exc.code == "submitting" else resolve.nothing_text(
                resolve.state_of(tap.app.conn, a.subject))  # fmt: skip
        return "Re-sent the proposal card"
    return resend(tap.app, a.subject, tap.actor)


@action("queue_sort", arg=SortArg)
async def queue_sort(tap: Tap) -> str:
    a: SortArg = tap.arg
    ui_send.edit(tap.app, queue.view(tap.app, a.page, a.sort), message_id=tap.message_id)
    return ""
