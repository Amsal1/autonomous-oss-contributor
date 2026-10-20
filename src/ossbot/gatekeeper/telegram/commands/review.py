"""Review commands (DESIGN §14.14.1): ``/queue`` and ``/approve [cid]``.

``/approve <cid>`` never approves anything: it re-sends the contribution's open cards through
``AppContext.reissue(cid, actor, keep_expiry=True)`` (§14.18.4 deltas 5 and 7), with the eligibility
answers of §14.18.4.3. Without a cid it shows the queue.
"""

from __future__ import annotations

import json
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
from ossbot.gatekeeper.telegram.ui import kit as k
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
    family: Literal["a", "p", "s", "r"]
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
    if a.family == "r":
        return resend_rescope(tap.app, a.subject)
    if a.family == "p":
        try:
            tap.app.reissue_proposal(a.subject, tap.actor, keep_expiry=True)
        except ApprovalError as exc:
            return "Submitting… try again in a moment" if exc.code == "submitting" else resolve.nothing_text(
                resolve.state_of(tap.app.conn, a.subject))  # fmt: skip
        return "Re-sent the proposal card"
    return resend(tap.app, a.subject, tap.actor)


def resend_rescope(app: AppContext, cid: str) -> str:
    """The rescope or ceiling-hit card again (it has no approval yet; its buttons check the state)."""
    from ossbot.gatekeeper.telegram import rescope_cards  # noqa: PLC0415 - rescope_cards imports review actions

    row = db.one(app.conn, "SELECT state, size_report_json FROM contributions WHERE id = ?", (cid,))
    if row is None or row["state"] != "RESCOPE_REQUIRED":
        return resolve.nothing_text(resolve.state_of(app.conn, cid))
    report = json.loads(row["size_report_json"] or "{}")
    reason: rescope_cards.Reason = "ceiling" if report.get("rescope_reason") == "ceiling" else "overrun"
    rescope_cards.send(app, cid, int(report.get("rev") or 1), reason, report)
    return "Re-sent the size card"


class ReviewOpenArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_id: str = Field(pattern=r"^a_[0-9A-Za-z]{10}$")


def _open_review(app: AppContext, approval_id: str) -> str:
    """The S2 explorer of an open approval card (read only: the explorer never approves)."""
    from ossbot.gatekeeper.telegram.publisher import CardPublisher  # noqa: PLC0415 - avoids a cycle
    from ossbot.gatekeeper.telegram.review.actions import open_explorer  # noqa: PLC0415

    row = db.one(app.conn, "SELECT 1 FROM approvals WHERE id = ? AND status IN ('pending','armed')", (approval_id,))
    if row is None:
        return "That card is no longer open."
    meta = CardPublisher(app).meta(approval_id)
    rev = meta.get("rev")
    open_explorer(app, meta["cid"], rev if isinstance(rev, int) else 1, meta.get("action_id"))
    return ""


@command("review", help="Explore an open card's changes: /review [cid]", safety="read")
async def review_cmd(ctx: CommandContext, args: list[str]) -> Reply:
    app = ctx.app
    sql = ("SELECT a.id, o.cid, o.kind FROM approvals a JOIN outbound_actions o ON o.id = a.action_id WHERE"
           " a.status IN ('pending','armed') AND o.kind IN ('open_pr','push_update')")  # fmt: skip
    if args:
        if not ID_RE.match(args[0]) or not args[0].startswith("c_"):
            return Reply("Usage: /review [c_0123456789]")
        row = db.one(app.conn, sql + " AND o.cid = ? ORDER BY a.created_at DESC LIMIT 1", (args[0],))
        if row is None:
            return Reply(esc(resolve.nothing_text(resolve.state_of(app.conn, args[0]))))
        return Reply(esc(_open_review(app, str(row["id"])) or "🧭 Explorer below."))
    rows = app.conn.execute(sql + " ORDER BY a.created_at LIMIT 8").fetchall()
    if not rows:
        return Reply("No open PR cards to review. /queue shows everything waiting.")
    btns = [k.button(f"🧭 {r['cid']}", "review_open", {"approval_id": r["id"]}) for r in rows]
    return Reply("", view=k.plain("picker", k.line("🧭 Review which card?"),
                                  rows=[btns[i : i + 2] for i in range(0, len(btns), 2)]))  # fmt: skip


@action("review_open", arg=ReviewOpenArg)
async def review_open(tap: Tap) -> str:
    return _open_review(tap.app, tap.arg.approval_id)


@action("queue_sort", arg=SortArg)
async def queue_sort(tap: Tap) -> str:
    a: SortArg = tap.arg
    ui_send.edit(tap.app, queue.view(tap.app, a.page, a.sort), message_id=tap.message_id)
    return ""
