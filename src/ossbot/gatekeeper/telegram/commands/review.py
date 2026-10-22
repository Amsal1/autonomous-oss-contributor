"""Review commands (DESIGN §14.14.1): ``/queue``, ``/approve [cid]``, ``/review [cid]`` and
``/reject [cid] [reason]`` (picker -> reasons -> confirm; the card's ❌ effects and 7-day repo cooldown).

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
from ossbot.gatekeeper.telegram.commands.common import cancel_button, confirm_button, confirm_panel
from ossbot.gatekeeper.telegram.commands.registry import CommandContext, Reply, command, confirm_handler
from ossbot.gatekeeper.telegram.escape import esc
from ossbot.gatekeeper.telegram.review import queue, resolve
from ossbot.gatekeeper.telegram.review import retry as review_retry
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
        if proposal is not None or resolve.state_of(app.conn, cid) == "PROPOSED":  # gate 1, card or not
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
    family: Literal["a", "p", "s", "r", "h", "f"]
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
    if a.family == "h":
        return resend_hold(tap.app, a.subject)
    if a.family == "f":
        return retry_failed(tap.app, a.subject, tap.actor)
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


def resend_hold(app: AppContext, cid: str) -> str:
    """The open egress hold's card again (its Resume is the two-tap on that card, I9)."""
    from ossbot.gatekeeper.telegram import hold_cards  # noqa: PLC0415 - hold_cards imports review modules

    hold = app.holds.get_open(cid)
    if hold is None:
        return hold_cards.GONE
    hold_cards.queue_hold_card(app, hold)
    return "Re-sent the hold card"


def retry_failed(app: AppContext, action_id: str, actor: str) -> str:
    """``SUBMIT_FAILED`` -> Open: the retry note's 🔁 Retry (a fresh card; approve and confirm again)."""
    if not action_id.startswith("x_"):
        return resolve.SUPERSEDED
    try:
        approval_id = review_retry.retry_failed(app, action_id, actor)
    except ApprovalError:
        row = db.one(app.conn, "SELECT cid FROM outbound_actions WHERE id = ?", (action_id,))
        return resolve.nothing_text(resolve.state_of(app.conn, str(row["cid"]) if row else ""))
    app.on_approval_created(approval_id)
    return "🔁 A fresh card follows (approve and confirm again)."


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


# ----------------------------------------------------------------------------- /reject (§14.14.1)
RejectReason = Literal["not_valuable", "wrong_approach", "repo_unsuitable", "other"]
REJECT_LABELS: dict[str, str] = {"not_valuable": "Not valuable", "wrong_approach": "Wrong approach",
                                 "repo_unsuitable": "Repo unsuitable (7d)", "other": "Other…"}  # fmt: skip
OPEN_CARDS_SQL = ("SELECT a.id, o.cid, o.kind FROM approvals a JOIN outbound_actions o ON o.id = a.action_id"
                  " WHERE a.status IN ('pending','armed')")  # fmt: skip


class RejectArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_id: str = Field(pattern=r"^a_[0-9A-Za-z]{10}$")
    reason: RejectReason | None = None


def _reason_of(words: list[str]) -> tuple[RejectReason, str]:
    """A typed reason: a key or label (``wrong_approach``, ``Wrong approach``), else free text = Other."""
    typed = " ".join(words).strip()
    norm = typed.lower().replace(" ", "_").rstrip("…")
    for key, label in REJECT_LABELS.items():
        if norm in {key, label.lower().replace(" ", "_").rstrip("…"), key.split("_", 1)[0]}:
            return key, ""  # type: ignore[return-value]
    return "other", typed[:500]


def _reject_reasons_view(app: AppContext, approval_id: str) -> k.View:
    row = db.one(app.conn, OPEN_CARDS_SQL + " AND a.id = ?", (approval_id,))
    if row is None:
        return k.plain("fyi", k.line("That card is no longer open."))
    btns = [k.button(label, "reject_cmd", {"approval_id": approval_id, "reason": key})
            for key, label in REJECT_LABELS.items()]  # fmt: skip
    return k.plain("picker", (k.Const("❌ "), k.Bold(k.Const("Reject ")), k.Code(k.Text(str(row["cid"]), 40)),
                              k.Const(f" ({row['kind']}): why?")),
                   rows=[btns[:2], btns[2:], [cancel_button()]])  # fmt: skip


def _reject_confirm_view(app: AppContext, approval_id: str, reason: RejectReason, text: str = "") -> k.View:
    row = db.one(app.conn, OPEN_CARDS_SQL + " AND a.id = ?", (approval_id,))
    if row is None:
        return k.plain("fyi", k.line("That card is no longer open."))
    lines: list[k.Line] = [(k.Const("❌ Reject "), k.Code(k.Text(str(row["cid"]), 40)),
                            k.Const(f" ({row['kind']}): {REJECT_LABELS[reason]}. Nothing is published;"
                                    " the card's buttons stop working."))]  # fmt: skip
    if reason == "repo_unsuitable":
        lines.append(k.line("No new proposals for this repo for 7 days."))
    if text:
        lines.append((k.Const("Reason: "), k.Text(text, 500)))
    arg = json.dumps({"approval_id": approval_id, "reason": reason, "text": text}, sort_keys=True)
    return confirm_panel(lines, confirm_button("❌ Reject", "reject_confirm", arg))


@command("reject", help="Reject an open card: /reject [cid] [reason]", safety="config")
async def reject_cmd(ctx: CommandContext, args: list[str]) -> Reply:
    """Picker -> reasons -> confirm (§14.14.1); nothing is rejected without the confirm tap."""
    app = ctx.app
    if not args:
        rows = app.conn.execute(OPEN_CARDS_SQL + " ORDER BY a.created_at LIMIT 8").fetchall()
        if not rows:
            return Reply("No open cards to reject. /queue shows everything waiting.")
        btns = [k.button(f"❌ {r['cid']} · {r['kind']}", "reject_cmd", {"approval_id": r["id"]}) for r in rows]
        return Reply("", view=k.plain("picker", k.line("❌ Reject which card?"),
                                      rows=[btns[i : i + 2] for i in range(0, len(btns), 2)]))  # fmt: skip
    cid = args[0].strip()
    if not ID_RE.match(cid) or not cid.startswith("c_"):
        return Reply("Usage: /reject [c_0123456789] [reason]")
    row = db.one(app.conn, OPEN_CARDS_SQL + " AND o.cid = ? ORDER BY a.created_at DESC LIMIT 1", (cid,))
    if row is None:
        return Reply(esc(resolve.nothing_text(resolve.state_of(app.conn, cid))))
    if len(args) == 1:
        return Reply("", view=_reject_reasons_view(app, str(row["id"])))
    reason, text = _reason_of(args[1:])
    return Reply("", view=_reject_confirm_view(app, str(row["id"]), reason, text))


@action("reject_cmd", arg=RejectArg)
async def reject_pick(tap: Tap) -> str:
    a: RejectArg = tap.arg
    view = (_reject_reasons_view(tap.app, a.approval_id) if a.reason is None
            else _reject_confirm_view(tap.app, a.approval_id, a.reason))  # fmt: skip
    ui_send.edit(tap.app, view, message_id=tap.message_id)
    return ""


@confirm_handler("reject_confirm")
async def reject_confirmed(ctx: CommandContext, arg: str | None) -> Reply:
    app = ctx.app
    try:
        req = json.loads(arg or "")
        approval_id, reason, text = str(req["approval_id"]), str(req["reason"]), str(req.get("text") or "")
    except (ValueError, KeyError, TypeError):
        return Reply("This button is no longer valid.")
    if reason not in REJECT_LABELS:
        return Reply("This button is no longer valid.")
    row = db.one(app.conn, OPEN_CARDS_SQL + " AND a.id = ?", (approval_id,))
    if row is None:
        return Reply("That card is no longer open; nothing was rejected.")
    cid = str(row["cid"])
    with db.transaction(app.conn):
        action_id = app.approvals.reject_open(approval_id, tg_user_id=ctx.tg_user_id, reason=reason)
        ctx.handlers.rejected(action_id, ctx.actor)
        if text:
            app.audit.append(ctx.actor, "rejection_reason", {"approval_id": approval_id, "reason": text}, cid)
    ctx.handlers.rejected_stub(approval_id, action_id)
    if reason == "repo_unsuitable":
        ctx.handlers.repo_cooldown(cid, ctx.actor)
    elif reason == "other" and not text:
        ctx.handlers.ask_reject_reason(cid, approval_id, ctx.message_id)
    return Reply(f"❌ Rejected {esc(cid)} ({esc(REJECT_LABELS[reason])}); nothing was published.")


@action("review_open", arg=ReviewOpenArg)
async def review_open(tap: Tap) -> str:
    return _open_review(tap.app, tap.arg.approval_id)


@action("queue_sort", arg=SortArg)
async def queue_sort(tap: Tap) -> str:
    a: SortArg = tap.arg
    ui_send.edit(tap.app, queue.view(tap.app, a.page, a.sort), message_id=tap.message_id)
    return ""
