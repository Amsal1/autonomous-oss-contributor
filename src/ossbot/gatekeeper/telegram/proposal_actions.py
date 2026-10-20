"""Gate 1 kit buttons and replies (DESIGN §14.11.15; PLAN item 46): Skip, Later, Never, Undo, Adjust,
Make it smaller, Details and Ask maintainers first.

Every kit token carries ``{cid, version}`` (``approval_id`` NULL) and resolves the open ``p_`` approval of
that version at tap time; a tap on an older version answers "Superseded…". The decisions themselves run
through ``ProposalService`` (``decline``/``undo``/``request_revision``), which goes through
``ProposalApprovals.reject``; ✅ Start work stays the decision token of the card (U3).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ossbot.common.clock import from_iso
from ossbot.common.control_flags import Blocked
from ossbot.common.ids import new_token
from ossbot.gatekeeper import db
from ossbot.gatekeeper.action_requests import ActionRefused, claim_body, request_action
from ossbot.gatekeeper.approvals import ApprovalError
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.proposals import SNOOZE_DAYS_MAX, ProposalRefused, ProposalService
from ossbot.gatekeeper.state_machine import TransitionError
from ossbot.gatekeeper.telegram import keyboards, proposal_cards
from ossbot.gatekeeper.telegram.actions import Tap, action
from ossbot.gatekeeper.telegram.escape import b, tg_time
from ossbot.gatekeeper.telegram.inputs import Typed, input_handler, view_reply
from ossbot.gatekeeper.telegram.review.resolve import CID_PATTERN
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send

UNDO_TTL = timedelta(minutes=10)
TOO_LATE = "Too late to undo; re-request with /work <issue url>"
ADJUST_PLACEHOLDER = "What should change in the plan? (≤ 2000)"
LATER_DAYS = (1, 3, 7, 14)


class ProposalArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cid: str = Field(pattern=CID_PATTERN)
    version: int = Field(ge=1, le=10_000)
    page: int | None = Field(default=None, ge=1, le=20)  # Details sections
    days: int | None = Field(default=None, ge=1, le=SNOOZE_DAYS_MAX)  # Later
    target: Literal["repo", "kind"] | None = None  # Never


class UndoArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cid: str = Field(pattern=CID_PATTERN)
    version: int = Field(ge=1, le=10_000)
    approval_id: str = Field(pattern=r"^p_[0-9A-HJKMNP-TV-Z]{10}$")


# ---------------------------------------------------------------------------- helpers
def open_approval(ctx: AppContext, cid: str, version: int) -> Any:
    return db.one(ctx.conn, "SELECT * FROM proposal_approvals WHERE cid = ? AND version = ? AND status IN"
                  " ('pending','armed') ORDER BY created_at DESC LIMIT 1", (cid, version))  # fmt: skip


def _current(ctx: AppContext, a: ProposalArg) -> tuple[Any, str | None]:
    """(the open approval of this version, or None with the refusal text)."""
    row = db.one(ctx.conn, "SELECT proposal_version, state FROM contributions WHERE id = ?", (a.cid,))
    if row is None or row["proposal_version"] != a.version:
        return None, proposal_cards.SUPERSEDED
    approval = open_approval(ctx, a.cid, a.version)
    if approval is None:
        last = db.one(ctx.conn, "SELECT status FROM proposal_approvals WHERE cid = ? AND version = ?"
                      " ORDER BY created_at DESC LIMIT 1", (a.cid, a.version))  # fmt: skip
        if last is not None and last["status"] == "expired":
            return None, proposal_cards.EXPIRED  # §14.11.15 Expiry
        return None, "This proposal is no longer open."
    return approval, None


def edit_card(ctx: AppContext, approval_id: str, note: str | None, markup: dict[str, Any] | None) -> None:
    """The proposal card's text (plus a status line) and keyboard, edited in place."""
    row = db.one(ctx.conn, "SELECT tg_chat_id, tg_message_id FROM proposal_approvals WHERE id = ?", (approval_id,))
    if row is None or row["tg_message_id"] is None:
        return
    text = ctx.decisions.proposals.card_view(approval_id).card + (f"\n\n{note}" if note else "")
    payload: dict[str, Any] = {"chat_id": row["tg_chat_id"], "message_id": row["tg_message_id"], "text": text,
                               "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}  # fmt: skip
    payload["reply_markup"] = markup if markup is not None else {"inline_keyboard": []}
    ctx.outbox.enqueue("editMessageText", payload, dedupe_key=f"view:{new_token()}", thread_key="reviews")


def _restore(ctx: AppContext, approval_id: str) -> None:
    svc = ctx.decisions.proposals
    tokens = svc.rotate_card_tokens(approval_id)
    edit_card(ctx, approval_id, None, keyboards.to_json(svc.card_rows(approval_id, tokens)))


def _row(ctx: AppContext, sql: str, params: tuple[Any, ...]) -> Any:
    """A row that must exist (the card's own approval or contribution)."""
    row = db.one(ctx.conn, sql, params)
    if row is None:
        raise ApprovalError("gone", "this card's item no longer exists")
    return row


def _mid(ctx: AppContext, approval_id: str) -> int:
    return int(_row(ctx, "SELECT tg_message_id FROM proposal_approvals WHERE id = ?", (approval_id,))[0])


def _kit(ctx: AppContext, approval_id: str, purpose: str, arg: dict[str, Any], *, single_use: bool = False,
         ttl: timedelta = proposal_cards.KIT_TTL) -> str:  # fmt: skip
    return proposal_cards.kit_token(ctx, purpose, message_id=_mid(ctx, approval_id), cid=str(arg["cid"]), arg=arg,
                                    single_use=single_use, ttl=ttl)  # fmt: skip


def _undo_markup(ctx: AppContext, approval_id: str, cid: str, version: int) -> dict[str, Any]:
    tok = _kit(ctx, approval_id, "proposal_undo", {"cid": cid, "version": version, "approval_id": approval_id},
               single_use=True, ttl=UNDO_TTL)  # fmt: skip
    return keyboards.to_json([[("↩️ Undo", tok)]])


def _decline(tap: Tap, approval: Any, reason: str, *, days: int | None = None) -> str | None:
    try:
        ProposalService(tap.app).decline(str(approval["id"]), reason, tap.actor, snooze_days=days)
    except ProposalRefused as exc:
        return "Cancel the Start-work confirm first" if exc.code == "cancel_confirm_first" else f"Refused: {exc.code}"
    except (ApprovalError, TransitionError) as exc:
        return f"Refused: {str(exc)[:150]}"
    return None


# ---------------------------------------------------------------------------- Skip / Later / Never / Undo
@action("proposal_skip", arg=ProposalArg)
async def skip(tap: Tap) -> str:
    approval, refused = _current(tap.app, tap.arg)
    if refused:
        return refused
    failed = _decline(tap, approval, "skipped")
    if failed:
        return failed
    edit_card(tap.app, str(approval["id"]), f"⏭ {b('Skipped')}",
              _undo_markup(tap.app, str(approval["id"]), tap.arg.cid, tap.arg.version))  # fmt: skip
    return "Skipped. Undo within 10 min."


@action("proposal_later", arg=ProposalArg)
async def later(tap: Tap) -> str:
    a: ProposalArg = tap.arg
    approval, refused = _current(tap.app, a)
    if refused:
        return refused
    aid = str(approval["id"])
    if a.days is not None:
        failed = _decline(tap, approval, "snoozed", days=a.days)
        if failed:
            return failed
        edit_card(tap.app, aid, snoozed_note(tap.app, a.days), _undo_markup(tap.app, aid, a.cid, a.version))
        return f"Snoozed for {a.days} d. Undo within 10 min."
    base = {"cid": a.cid, "version": a.version}
    picks: list[keyboards.Button] = [(f"{d} d", _kit(tap.app, aid, "proposal_later", {**base, "days": d}))
                                     for d in LATER_DAYS]  # fmt: skip
    rows: keyboards.Rows = [picks[:3], [picks[3], ("Custom…", _kit(tap.app, aid, "proposal_later_custom", base)),
                                        ("↩️ Back", _kit(tap.app, aid, "proposal_back", base))]]  # fmt: skip
    edit_card(tap.app, aid, f"⏰ {b('Later: for how long?')}", keyboards.to_json(rows))
    return ""


@action("proposal_later_custom", arg=ProposalArg)
async def later_custom(tap: Tap) -> str:
    approval, refused = _current(tap.app, tap.arg)
    if refused:
        return refused
    view = k.plain("confirm_prompt", k.line(f"⏰ Snooze {tap.arg.cid} for how many days? (1-{SNOOZE_DAYS_MAX})"))
    view.placeholder = f"Days (1-{SNOOZE_DAYS_MAX})"
    view.cid = tap.arg.cid
    view.state = {"prompt": {"kind": "proposal_snooze_days", "cid": tap.arg.cid, "proposal_id": str(approval["id"]),
                             "rev": tap.arg.version}}  # fmt: skip
    ui_send.deliver(tap.app, view, thread="reviews", reply_to=tap.message_id)
    return ""


@action("proposal_never", arg=ProposalArg)
async def never(tap: Tap) -> str:
    a: ProposalArg = tap.arg
    approval, refused = _current(tap.app, a)
    if refused:
        return refused
    aid = str(approval["id"])
    base = {"cid": a.cid, "version": a.version}
    kind = _row(tap.app, "SELECT kind FROM contributions WHERE id = ?", (a.cid,))["kind"]
    rows: keyboards.Rows = [[("🚫 This repo", _kit(tap.app, aid, "proposal_never_pick", {**base, "target": "repo"}))]]
    if kind:
        rows[0].append((f"🚫 Kind: {proposal_cards.kind_name(kind)}"[:24],
                        _kit(tap.app, aid, "proposal_never_pick", {**base, "target": "kind"})))  # fmt: skip
    rows.append([("↩️ Back", _kit(tap.app, aid, "proposal_back", base))])
    edit_card(tap.app, aid, f"🚫 {b('Never: what?')}", keyboards.to_json(rows))
    return ""


@action("proposal_never_pick", arg=ProposalArg)
async def never_pick(tap: Tap) -> str:
    a: ProposalArg = tap.arg
    approval, refused = _current(tap.app, a)
    if refused or a.target is None:
        return refused or proposal_cards.SUPERSEDED
    aid = str(approval["id"])
    c = _row(tap.app, "SELECT upstream, kind FROM contributions WHERE id = ?", (a.cid,))
    base = {"cid": a.cid, "version": a.version, "target": a.target}
    if a.target == "repo":
        text = f"Never propose work from {c['upstream']} again? Undo in /repos."
        label = "🚫 Never this repo"
    else:
        text = (f'Never propose "{proposal_cards.kind_name(c["kind"])}" work (kind {c["kind"] or ""}) in'
                " any repo again? Undo in /filters → Kinds.")  # fmt: skip
        label = "🚫 Never this kind"
    rows: keyboards.Rows = [[(label, _kit(tap.app, aid, "proposal_never_confirm", base, single_use=True), "danger"),
                             ("↩️ Cancel", _kit(tap.app, aid, "proposal_back", base))]]  # fmt: skip
    edit_card(tap.app, aid, b(text), keyboards.to_json(rows))
    return ""


@action("proposal_never_confirm", arg=ProposalArg)
async def never_confirm(tap: Tap) -> str:
    a: ProposalArg = tap.arg
    approval, refused = _current(tap.app, a)
    if refused or a.target is None:
        return refused or proposal_cards.SUPERSEDED
    c = _row(tap.app, "SELECT kind FROM contributions WHERE id = ?", (a.cid,))
    failed = _decline(tap, approval, "never_repo" if a.target == "repo" else "never_kind")
    if failed:
        return failed
    what = "this repo" if a.target == "repo" else proposal_cards.kind_name(c["kind"])
    edit_card(tap.app, str(approval["id"]), f"🚫 {b('Never: ' + what)}", None)
    return f"Never: {what}."


@action("proposal_back", arg=ProposalArg)
async def back(tap: Tap) -> str:
    approval, refused = _current(tap.app, tap.arg)
    if refused:
        return refused
    _restore(tap.app, str(approval["id"]))
    return ""


@action("proposal_undo", arg=UndoArg)
async def undo(tap: Tap) -> str:
    a: UndoArg = tap.arg
    ctx = tap.app
    declined = db.one(ctx.conn, "SELECT ts FROM audit_events WHERE event = 'proposal_declined' AND cid = ?"
                      " ORDER BY seq DESC LIMIT 1", (a.cid,))  # fmt: skip
    if declined is None or from_iso(declined["ts"]) + UNDO_TTL < ctx.clock.now():
        return TOO_LATE
    try:
        ProposalService(ctx).undo(a.cid, a.approval_id, tap.actor)
    except (ProposalRefused, TransitionError, ApprovalError):
        return TOO_LATE
    edit_card(ctx, a.approval_id, f"↩️ {b('Undone')} · see the card below", None)
    return "↩️ Undone: a fresh card follows."


# ---------------------------------------------------------------------------- Adjust / Smaller
def _revising(ctx: AppContext, approval_id: str, version: int) -> None:
    markup = keyboards.disabled_markup(f"⛔ Revising v{version} → v{version + 1}…")
    edit_card(ctx, approval_id, None, markup)


@action("proposal_adjust", arg=ProposalArg)
async def adjust(tap: Tap) -> str:
    approval, refused = _current(tap.app, tap.arg)
    if refused:
        return refused
    prompt_adjust(tap.app, tap.arg.cid, tap.arg.version, str(approval["id"]), reply_to=tap.message_id,
                  text=f"✏️ What should change in the plan of {tap.arg.cid} v{tap.arg.version}?")  # fmt: skip
    return "Reply with what should change."


def prompt_adjust(ctx: AppContext, cid: str, version: int, approval_id: str | None, *, reply_to: int | None,
                  text: str, placeholder: str = ADJUST_PLACEHOLDER) -> None:  # fmt: skip
    view = k.plain("confirm_prompt", k.line(text))
    view.placeholder = placeholder
    view.cid = cid
    view.state = {"prompt": {"kind": "proposal_adjust", "cid": cid, "proposal_id": approval_id, "rev": version}}
    ui_send.deliver(ctx, view, thread="reviews", reply_to=reply_to)


def revise(ctx: AppContext, cid: str, version: int, kind: Literal["adjust", "smaller", "split"], text: str,
           actor: str) -> str:  # fmt: skip
    try:
        ProposalService(ctx).request_revision(cid, version, kind, text, actor)
    except ProposalRefused as exc:
        if exc.code == "already_smallest":
            return "Already the smallest class."
        return proposal_cards.SUPERSEDED if exc.code == "superseded" else f"Not sent: {exc.code}"
    approval = open_approval(ctx, cid, version)
    if approval is not None:
        _revising(ctx, str(approval["id"]), version)
    return f"✏️ Sent to scout; v{version + 1} follows."


@action("proposal_smaller", arg=ProposalArg)
async def smaller(tap: Tap) -> str:
    _, refused = _current(tap.app, tap.arg)
    if refused:
        return refused
    return revise(tap.app, tap.arg.cid, tap.arg.version, "smaller", "", tap.actor)


@input_handler("proposal_adjust")
async def on_adjust(t: Typed) -> None:
    p = t.prompt
    assert p is not None
    assert p.cid is not None
    row = db.one(t.app.conn, "SELECT proposal_version FROM contributions WHERE id = ?", (p.cid,))
    version = int(row["proposal_version"]) if row and row["proposal_version"] else int(p.rev or 1)
    _say(t, revise(t.app, p.cid, version, "adjust", t.text, t.actor))


@view_reply("proposal")
async def on_card_reply(t: Typed) -> None:
    """Rule 5: a reply directly to a proposal card is ✏️ Adjust plan on that version."""
    assert t.view is not None
    cid, version = str(t.view["cid"]), t.view["rev"]
    if not isinstance(version, int):
        return
    _say(t, revise(t.app, cid, version, "adjust", t.text, t.actor))


@input_handler("proposal_snooze_days")
async def on_snooze_days(t: Typed) -> None:
    p = t.prompt
    assert p is not None
    text = t.text.strip()
    if not text.isdigit() or not 1 <= int(text) <= SNOOZE_DAYS_MAX:
        _say(t, f"Not snoozed: send a whole number of days, 1-{SNOOZE_DAYS_MAX}.")
        return
    approval = db.one(t.app.conn, "SELECT * FROM proposal_approvals WHERE id = ? AND status = 'pending'",
                      (p.proposal_id,))  # fmt: skip
    if approval is None or p.cid is None:
        _say(t, "This proposal is no longer open.")
        return
    try:
        ProposalService(t.app).decline(str(approval["id"]), "snoozed", t.actor, snooze_days=int(text))
    except (ProposalRefused, ApprovalError, TransitionError) as exc:
        _say(t, f"Not snoozed: {str(exc)[:150]}")
        return
    aid = str(approval["id"])
    edit_card(t.app, aid, snoozed_note(t.app, int(text)), _undo_markup(t.app, aid, p.cid, int(approval["version"])))
    _say(t, f"Snoozed for {text} d.")


def snoozed_note(ctx: AppContext, days: int) -> str:
    until = ctx.clock.now() + timedelta(days=days)
    return f"⏰ {b('Snoozed until')} {tg_time(int(until.timestamp()), until.strftime('%d %b'))}"


def _say(t: Typed, text: str) -> None:
    ui_send.deliver(t.app, k.plain("fyi", (k.Const(text[:500]),)), thread_id=t.thread_id, reply_to=t.message_id)


# ---------------------------------------------------------------------------- Details / Ask first
@action("proposal_details", arg=ProposalArg)
async def details(tap: Tap) -> str:
    a: ProposalArg = tap.arg
    row = db.one(tap.app.conn, "SELECT proposal_version FROM contributions WHERE id = ?", (a.cid,))
    if row is None or row["proposal_version"] != a.version:
        return proposal_cards.SUPERSEDED
    view = proposal_cards.details_view(tap.app, a.cid, a.version, a.page or 1)
    ui_send.deliver(tap.app, view, thread="reviews", reply_to=tap.message_id, silent=True)
    return ""


@action("proposal_details_nav", arg=ProposalArg)
async def details_nav(tap: Tap) -> str:
    a: ProposalArg = tap.arg
    row = db.one(tap.app.conn, "SELECT proposal_version FROM contributions WHERE id = ?", (a.cid,))
    if row is None or row["proposal_version"] != a.version:
        return proposal_cards.SUPERSEDED
    ui_send.edit(tap.app, proposal_cards.details_view(tap.app, a.cid, a.version, a.page or 1),
                 message_id=tap.message_id)  # fmt: skip
    return ""


@action("proposal_ask_first", arg=ProposalArg)
async def ask_first(tap: Tap) -> str:
    """🙋 Ask maintainers first: an ``issue_claim`` action whose own S1/S8 card follows; the proposal stays."""
    a: ProposalArg = tap.arg
    _, refused = _current(tap.app, a)
    if refused:
        return refused
    ctx = tap.app
    c = db.one(ctx.conn, "SELECT issue_number FROM contributions WHERE id = ?", (a.cid,))
    if c is None or not c["issue_number"]:
        return "No issue to ask on."
    body = claim_body(ctx.config.profile.github_login)
    try:  # the same path as POST /v1/actions: marker, normalization, policy check, running check
        done = request_action(ctx, a.cid, "issue_claim", int(c["issue_number"]), body, tap.actor)
    except Blocked:
        return "Paused or killed: nothing was requested. /resume first."
    except ActionRefused as exc:
        return f"Not requested: {exc.code}"
    if done.reused:
        return "A claim card is already waiting."
    assert done.approval_id is not None
    with db.transaction(ctx.conn):
        ctx.audit.append(tap.actor, "claim_requested", {"action_id": done.action_id}, a.cid)
    ctx.on_approval_created(done.approval_id)
    return "🙋 The claim card follows (approve it to post)."
