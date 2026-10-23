"""🔧 Fix this / 🙈 Ignore on a review-bot finding card (DESIGN §14.11.11; ADR-0003 addendum 2026-10-23, Part B).

``🔧 Fix this`` (``pr_bot_fix``, single use) runs the same pre-check as ``/fix`` and answers with a separate
one-tap confirm (never rich, U3): ``[🔧 Start fix round]`` (``pr_bot_fix_confirm``) and ``[↩️ Back]``
(``pr_bot_back``), both single use, answering either retires both. Only the Start tap writes: the gatekeeper
re-fetches the inline comment with its own read client (I12 spirit; never the card's text), checks it is a
bot comment on this PR with a body, and calls ``owner_fix.start_round`` with the FIXED instruction
``owner_fix.BOT_INSTRUCTION`` and the finding as a ``review_bot`` change request: the body is stored in
``change_requests.untrusted_text`` and reaches the coder only inside ``<untrusted>``, never in
``<human_instruction>`` (I8). It counts as an owner fix round and the update arrives as a normal
``push_update`` card that needs its own two taps (I1). No GitHub write, no thread reply, no resolve.

``🙈 Ignore`` (``pr_bot_ignore``) only edits the card and audits ``bot_finding_ignored``.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ossbot.common.clock import to_iso
from ossbot.common.github_bots import is_bot
from ossbot.gatekeeper import db, owner_fix
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.actions import Tap, action
from ossbot.gatekeeper.telegram.bot_finding_cards import finding_view, resolved_view, where
from ossbot.gatekeeper.telegram.commands.fix import refusal_text
from ossbot.gatekeeper.telegram.review.resolve import CID_PATTERN
from ossbot.gatekeeper.telegram.ui import ids
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send

PATH_RE = re.compile(r"[A-Za-z0-9._/@+-]{1,200}")
GONE = "the finding is gone or isn't a review-bot comment on PR #{n}"
NO_READER = "GitHub reads are unavailable; try again later"
CONFIRM_PURPOSES = ("pr_bot_fix_confirm", "pr_bot_back")


class FindingArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cid: str = Field(pattern=CID_PATTERN)
    comment_id: int = Field(ge=1)


class ConfirmArg(FindingArg):
    card: int = Field(ge=1)  # the finding card's message id (gatekeeper-built)


def _row(ctx: AppContext, cid: str) -> Any:
    return db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))


def _card_finding(ctx: AppContext, cid: str, message_id: int) -> dict[str, Any] | None:
    """The finding the card was sent with (its ``tg_views.state``), or None."""
    row = db.one(ctx.conn, "SELECT state_json FROM tg_views WHERE message_id = ? AND cid = ?", (message_id, cid))
    if row is None:
        return None
    try:
        state = json.loads(row["state_json"] or "{}")
    except ValueError:
        return None
    finding = state.get("finding") if isinstance(state, dict) else None
    return finding if isinstance(finding, dict) and isinstance(finding.get("id"), int) else None


def _retire_card(ctx: AppContext, chat_id: int, card: int) -> None:
    """The card's own buttons die before it is re-rendered (an edit does not retire old tokens)."""
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE ui_tokens SET used_at = ? WHERE tg_chat_id = ? AND tg_message_id = ? AND"
                         " purpose IN ('pr_bot_fix', 'pr_bot_ignore') AND used_at IS NULL",
                         (to_iso(ctx.clock.now()), chat_id, card))  # fmt: skip


def _rearm(ctx: AppContext, chat_id: int, cid: str, card: int) -> None:
    """Re-send the card's buttons with fresh tokens (the tapped one was single use)."""
    row, finding = _row(ctx, cid), _card_finding(ctx, cid, card)
    if row is not None and finding is not None and row["state"] == "WATCHING":
        _retire_card(ctx, chat_id, card)
        ui_send.edit(ctx, finding_view(dict(row), finding), message_id=card)


def _retire(tap: Tap) -> None:
    with db.transaction(tap.app.conn):
        tap.app.conn.execute("UPDATE ui_tokens SET used_at = ? WHERE tg_chat_id = ? AND tg_message_id = ? AND"
                             " purpose IN ('pr_bot_fix_confirm', 'pr_bot_back') AND used_at IS NULL",
                             (to_iso(tap.app.clock.now()), tap.chat_id, tap.message_id))  # fmt: skip


def _note(text: str) -> k.View:
    return k.View(kind="fyi", blocks=[k.Block((k.Lines((ids.spans(text),)),))])


def confirm_view(ctx: AppContext, row: Any, finding: dict[str, Any], card: int) -> k.View:
    """The one-tap confirm (never rich, U3)."""
    cid = str(row["id"])
    limit = ctx.config.policy.limits.owner_fix_rounds_max
    ttl = timedelta(seconds=ctx.config.policy.limits.confirm_ttl_seconds)
    arg = {"cid": cid, "comment_id": int(finding["id"]), "card": card}
    lines: tuple[k.Line, ...] = (
        (k.Const("🔧 "), k.Bold(k.Const("Fix this finding?")), k.Const(f" · PR #{row['pr_number']} · "),
         k.Text(where(finding), 160), k.Const(" · "), k.Id(cid)),
        k.line(f"Round {int(row['owner_fix_rounds'] or 0) + 1} of {limit} · the coder checks it against the current"
               " code first; nothing is pushed before you approve the update card."),
    )  # fmt: skip
    rows: k.Rows = [[k.button("🔧 Start fix round", "pr_bot_fix_confirm", arg, style="primary", single_use=True,
                              ttl=ttl),
                     k.button("↩️ Back", "pr_bot_back", arg, single_use=True, ttl=ttl)]]  # fmt: skip
    return k.View(kind="confirm_prompt", cid=cid, blocks=[k.Block((k.Lines(lines),))], rows=rows)


@action("pr_bot_fix", arg=FindingArg)
async def pr_bot_fix(tap: Tap) -> str:
    cid = tap.arg.cid
    code = owner_fix.refusal(tap.app, cid)
    finding = _card_finding(tap.app, cid, tap.message_id)
    if code is None and finding is None:
        code = "not_found"
    if code is not None:
        _rearm(tap.app, tap.chat_id, cid, tap.message_id)
        return k.toast(f"Not started: {refusal_text(tap.app, code, cid)}.")
    assert finding is not None
    view = confirm_view(tap.app, _row(tap.app, cid), finding, tap.message_id)
    ui_send.deliver(tap.app, view, thread_id=tap.thread_id, reply_to=tap.message_id)
    return "Confirm to start the fix round."


def _finding_of(doc: dict[str, Any] | None, row: Any, comment_id: int) -> owner_fix.Finding | None:
    """The re-fetched comment as a Finding, or None unless it is a bot comment with a body on this PR."""
    if not isinstance(doc, dict) or doc.get("id") != comment_id or not is_bot(doc.get("user")):
        return None
    expected = f"https://api.github.com/repos/{row['upstream']}/pulls/{row['pr_number']}".lower()
    if str(doc.get("pull_request_url") or "").lower() != expected:
        return None
    body = doc.get("body")
    if not isinstance(body, str) or not body.strip():
        return None
    path = str(doc.get("path") or "")
    line = doc.get("line") or doc.get("original_line")
    author = str((doc.get("user") or {}).get("login") or "")[:40]
    return owner_fix.Finding(comment_id, path if PATH_RE.fullmatch(path) else "?",
                             line if isinstance(line, int) and not isinstance(line, bool) else None, author,
                             body[:owner_fix.UNTRUSTED_MAX])  # fmt: skip


@action("pr_bot_fix_confirm", arg=ConfirmArg, effect="io")
async def pr_bot_fix_confirm(tap: Tap) -> str:
    """The human decision: re-fetch the finding (GET only), then one review_bot change request plus the human
    edge, no LLM, no GitHub write."""
    _retire(tap)
    ctx, cid, card = tap.app, tap.arg.cid, tap.arg.card
    row = _row(ctx, cid)
    reader = getattr(tap.handlers, "reader", None)
    reason: str | None = None
    finding: owner_fix.Finding | None = None
    if row is None:
        reason = refusal_text(ctx, "not_found", cid)
    elif reader is None:
        reason = NO_READER
    else:
        finding = _finding_of(await reader.review_comment(str(row["upstream"]), tap.arg.comment_id), row,
                              tap.arg.comment_id)  # fmt: skip
        if finding is None:
            reason = GONE.format(n=row["pr_number"])
    if finding is None:
        ui_send.edit(ctx, _note(f"Not started: {reason}."), message_id=tap.message_id)
        _rearm(ctx, tap.chat_id, cid, card)
        return "Not started."
    try:
        done = owner_fix.start_round(ctx, cid, owner_fix.BOT_INSTRUCTION, tap.actor, finding=finding)
    except owner_fix.FixRefused as exc:
        ui_send.edit(ctx, _note(f"Not started: {refusal_text(ctx, exc.code, cid)}."), message_id=tap.message_id)
        _rearm(ctx, tap.chat_id, cid, card)
        return "Not started."
    shown = {"id": finding.comment_id, "who": finding.author, "path": finding.path, "line": finding.line}
    started: tuple[k.Line, ...] = (
        (k.Const("🔧 "), k.Bold(k.Const(f"Fix round {done.round}/{done.max} started")), k.Const(" · "),
         k.Text(where(shown), 160), k.Const(" · "), k.Id(cid)),
        k.line("The update card arrives in ✅ Reviews; nothing is pushed before you approve it."),
    )  # fmt: skip
    ui_send.edit(ctx, k.View(kind="fyi", cid=cid, blocks=[k.Block((k.Lines(started),))]), message_id=tap.message_id)
    card_finding = _card_finding(ctx, cid, card) or {**shown, "body": ""}
    _retire_card(ctx, tap.chat_id, card)
    ui_send.edit(ctx, resolved_view(dict(row), card_finding, f"🔧 Fix round {done.round}/{done.max} started"),
                 message_id=card)  # fmt: skip
    return "🔧 Fix round started."


@action("pr_bot_back", arg=ConfirmArg)
async def pr_bot_back(tap: Tap) -> str:
    """Not now: the confirm closes and the card gets fresh buttons (no GitHub read)."""
    _retire(tap)
    ui_send.edit(tap.app, k.plain("fyi", k.line("Not started.")), message_id=tap.message_id)
    _rearm(tap.app, tap.chat_id, tap.arg.cid, tap.arg.card)
    return ""


@action("pr_bot_ignore", arg=FindingArg)
async def pr_bot_ignore(tap: Tap) -> str:
    ctx, cid = tap.app, tap.arg.cid
    row = _row(ctx, cid)
    with db.transaction(ctx.conn):
        ctx.audit.append(tap.actor, "bot_finding_ignored", {"comment_id": tap.arg.comment_id}, cid)
    finding = _card_finding(ctx, cid, tap.message_id)
    _retire_card(ctx, tap.chat_id, tap.message_id)
    if row is not None and finding is not None:
        ui_send.edit(ctx, resolved_view(dict(row), finding, "🙈 Ignored"), message_id=tap.message_id)
    return "🙈 Ignored."
