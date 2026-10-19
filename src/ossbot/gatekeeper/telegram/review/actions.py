"""Review-surface buttons and typed input (DESIGN §14.11.2-§14.11.12, §14.10.4 rules 3 and 5).

Every handler resolves the tap's action first (``resolve.refusal``) and acts only on what the outcome
table allows (§14.18.2): navigation stays read-only after a submit; comments and sends answer
"Submitted…"; anything on a dead action answers "Superseded…". Approve and Confirm are never here (U3).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalError
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.actions import NoArg, Tap, action
from ossbot.gatekeeper.telegram.inputs import Typed, input_handler, view_reply
from ossbot.gatekeeper.telegram.review import comments, docs, edit_text, explorer, interdiff_view, patchview, resolve
from ossbot.gatekeeper.telegram.review.resolve import ReviewArg, StaleShowCurrentArg
from ossbot.gatekeeper.telegram.runtime import ui_of
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send
from ossbot.gatekeeper.telegram.ui.sanitize import undefuse

log = logging.getLogger(__name__)
MOVED = {"explorer": "🧭 moved ↓", "file": "🧾 moved ↓", "interdiff": "🔀 moved ↓"}
FILE_REUSE_MESSAGES = 20  # §14.11.3: edit in place while fewer than 20 messages followed it in its thread


# ---------------------------------------------------------------------------- helpers
def card_message(ctx: AppContext, action_id: str | None) -> int | None:
    """The message id of the action's newest card (the reply target of its views)."""
    if action_id is None:
        return None
    row = db.one(ctx.conn, "SELECT tg_message_id FROM approvals WHERE action_id = ? AND tg_message_id IS NOT NULL"
                 " ORDER BY created_at DESC LIMIT 1", (action_id,))  # fmt: skip
    return int(row["tg_message_id"]) if row else None


def reviews_seq(ctx: AppContext) -> int:
    thread = ui_of(ctx).topics.thread_id("reviews")
    key = f"{ctx.telegram_chat_id}:{thread or 0}"
    row = db.one(ctx.conn, "SELECT n FROM tg_thread_seq WHERE thread_key = ?", (key,))
    return int(row["n"]) if row else 0


def _subject(ctx: AppContext, cid: str, rev: int, action_id: str | None) -> explorer.Subject:
    return explorer.subject(ctx.conn, ctx.gitops.derived_dir(cid, rev), cid, rev, action_id)


def _live_view(ctx: AppContext, kind: str, *, action_id: str | None, cid: str, rev: int) -> Any:
    return db.one(ctx.conn, "SELECT * FROM tg_views WHERE kind = ? AND cid = ? AND rev = ? AND stale = 0"
                  " AND (action_id IS ? OR action_id = ?) ORDER BY updated_at DESC LIMIT 1",
                  (kind, cid, rev, action_id, action_id))  # fmt: skip


def stub(ctx: AppContext, message_id: int, text: str, reason: str) -> None:
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE tg_views SET stale = 1 WHERE message_id = ?", (message_id,))
        ctx.outbox.enqueue("editMessageText", {"chat_id": ctx.telegram_chat_id, "message_id": message_id,
                                               "text": text}, dedupe_key=f"stub:{message_id}:{reason}")  # fmt: skip


def show_view(ctx: AppContext, view: k.View, *, existing: Any, reuse: bool) -> None:
    """Edit the live view in place, or send a new one as a reply to the card and stub the old one."""
    if existing is not None and reuse:
        ui_send.edit(ctx, view, message_id=int(existing["message_id"]))
        return
    if existing is not None:
        stub(ctx, int(existing["message_id"]), MOVED.get(view.kind, "moved ↓"), "moved")
    ui_send.deliver(ctx, view, thread="reviews", reply_to=card_message(ctx, view.action_id), silent=True)


def open_explorer(ctx: AppContext, cid: str, rev: int, action_id: str | None, section: int = 1, *,
                  in_place: bool = False) -> None:  # fmt: skip
    view = explorer.explorer_view(_subject(ctx, cid, rev, action_id), section)
    existing = _live_view(ctx, "explorer", action_id=action_id, cid=cid, rev=rev)
    show_view(ctx, view, existing=existing, reuse=in_place)


def open_file(ctx: AppContext, cid: str, rev: int, action_id: str | None, index: int, page: int = 1, *,
              picker_page: int | None = None, hunk_picker: int | None = None) -> None:  # fmt: skip
    """One file view per (cid, rev): reused while fewer than 20 messages followed it (§14.11.3, AC 52)."""
    existing = _live_view(ctx, "file", action_id=action_id, cid=cid, rev=rev)
    seq_now = reviews_seq(ctx)
    reuse = False
    seq = seq_now
    if existing is not None:
        state = json.loads(existing["state_json"] or "{}")
        reuse = seq_now - int(state.get("seq", 0)) < FILE_REUSE_MESSAGES
        if reuse:
            seq = int(state.get("seq", 0))
    view = explorer.file_view(_subject(ctx, cid, rev, action_id), index, page, seq=seq + (0 if reuse else 1),
                              picker_page=picker_page, hunk_picker=hunk_picker)  # fmt: skip
    # every path opened in this (cid, rev): the S8 "Reviewed" line counts them (one message is reused)
    seen = set(json.loads(existing["state_json"] or "{}").get("seen", [])) if existing is not None else set()
    view.state["seen"] = sorted(seen | ({view.path} if view.path else set()))
    show_view(ctx, view, existing=existing, reuse=reuse)


def _card_meta(tap: Tap) -> dict[str, Any]:
    from ossbot.gatekeeper.telegram.publisher import CardPublisher  # noqa: PLC0415 - avoids a cycle

    return CardPublisher(tap.app).meta(str(tap.row["approval_id"]))


def _rev(meta: dict[str, Any]) -> int:
    rev = meta.get("rev")
    return rev if isinstance(rev, int) else 1


# ------------------------------------------------------------------------ card view purposes
@action("explore", arg=NoArg)
async def explore(tap: Tap) -> str:
    meta = _card_meta(tap)
    open_explorer(tap.app, meta["cid"], _rev(meta), meta.get("action_id"))
    return ""


@action("files", arg=NoArg)
async def files(tap: Tap) -> str:
    meta = _card_meta(tap)
    open_file(tap.app, meta["cid"], _rev(meta), meta.get("action_id"), 0)
    return ""


@action("interdiff", arg=NoArg)
async def interdiff(tap: Tap) -> str:
    meta = _card_meta(tap)
    view = interdiff_view.view(tap.app, meta["cid"], _rev(meta), meta.get("action_id"))
    existing = _live_view(tap.app, "interdiff", action_id=meta.get("action_id"), cid=meta["cid"], rev=_rev(meta))
    show_view(tap.app, view, existing=existing, reuse=False)
    return ""


@action("edit_text_pick", arg=NoArg)
async def edit_pick(tap: Tap) -> str:
    meta = _card_meta(tap)
    edit_text.open_picker(tap.app, meta["cid"], _rev(meta), meta.get("action_id"), str(meta.get("kind")),
                          card_message=tap.message_id)  # fmt: skip
    return ""


@action("changes", arg=NoArg)
async def changes(tap: Tap) -> str:
    """✏️ Request changes: a ForceReply; the draft review's comments are included automatically."""
    meta = _card_meta(tap)
    comments.prompt(tap.app, "changes", meta["cid"], _rev(meta), meta.get("action_id"),
                    "✏️ Describe the change for " + meta["cid"] + " (≤ 4,000 chars). Draft comments are included.",
                    comments.CHANGES_PLACEHOLDER, reply_to=tap.message_id)  # fmt: skip
    return "Reply with what should change."


# --------------------------------------------------------------------------- navigation
@action("nav", arg=ReviewArg)
async def nav(tap: Tap) -> str:
    a: ReviewArg = tap.arg
    if a.rev is None:
        return resolve.SUPERSEDED
    refused = resolve.refusal(tap.app.conn, a.action_id, "navigation",
                              view_stale=resolve.view_is_stale(tap.app.conn, tap.message_id))  # fmt: skip
    if refused:
        return refused
    ctx = tap.app
    if a.view in {"explorer", "explorer_pick"}:
        # the section pager carries the section as its page; the Files list pager carries both
        section = a.section if a.section is not None else (a.page or 1)
        page = (a.page or 1) if a.section is not None else 1
        view = explorer.explorer_view(_subject(ctx, a.cid, a.rev, a.action_id), section, page,
                                      picker=a.view == "explorer_pick")  # fmt: skip
        ui_send.edit(ctx, view, message_id=tap.message_id)
    elif a.view == "file":
        open_file(ctx, a.cid, a.rev, a.action_id, a.index or 0, a.page or 1)
    elif a.view == "file_picker":
        open_file(ctx, a.cid, a.rev, a.action_id, a.index or 0, picker_page=a.page or 1)
    elif a.view == "hunk_picker":
        if resolve.refusal(ctx.conn, a.action_id, "comment"):
            return resolve.refusal(ctx.conn, a.action_id, "comment") or ""
        open_file(ctx, a.cid, a.rev, a.action_id, a.index or 0, hunk_picker=a.page or 1)
    elif a.view in {"doc_patch", "doc_html", "doc_body"}:
        tap.handlers.spawn(docs.send(ctx, a.view, a.cid, a.rev, tap.thread_id), "render")
        return "⏳ Preparing the document…"
    elif a.view == "interdiff":
        view = interdiff_view.view(ctx, a.cid, a.rev, a.action_id, a.page or 1)
        ui_send.edit(ctx, view, message_id=tap.message_id)
    elif a.view == "draft_all":
        ui_send.edit(ctx, comments.draft_view(ctx, a.cid, a.rev, a.action_id, show_all=True),
                     message_id=tap.message_id)  # fmt: skip
    return ""


@action("file_pick", arg=ReviewArg)
async def file_pick(tap: Tap) -> str:
    a: ReviewArg = tap.arg
    refused = resolve.refusal(tap.app.conn, a.action_id, "navigation",
                              view_stale=resolve.view_is_stale(tap.app.conn, tap.message_id))  # fmt: skip
    if refused or a.rev is None:
        return refused or resolve.SUPERSEDED
    open_file(tap.app, a.cid, a.rev, a.action_id, a.index or 0)
    return ""


# ----------------------------------------------------------------------------- comments
@action("comment_hunk", arg=ReviewArg)
async def comment_general(tap: Tap) -> str:
    a: ReviewArg = tap.arg
    refused = resolve.refusal(tap.app.conn, a.action_id, "comment")
    if refused or a.rev is None:
        return refused or resolve.SUPERSEDED
    comments.prompt(tap.app, "comment", a.cid, a.rev, a.action_id, f"💬 General comment on {a.cid} r{a.rev}",
                    comments.COMMENT_PLACEHOLDER, reply_to=tap.message_id)  # fmt: skip
    return ""


@action("comment_line_pick", arg=ReviewArg)
async def comment_hunk(tap: Tap) -> str:
    """A hunk (or the whole file) from the hunk picker, or a line from the ambiguous-quote picker."""
    a: ReviewArg = tap.arg
    refused = resolve.refusal(tap.app.conn, a.action_id, "comment")
    if refused or a.rev is None:
        return refused or resolve.SUPERSEDED
    if a.text is not None and a.line is not None:  # the line picker of an ambiguous quote
        saved = comments.save(tap.app, a.cid, a.rev, a.action_id, a.text, path=a.path, side=a.side or "new",
                              line_start=a.line, line_end=a.line, actor=tap.actor)  # fmt: skip
        return saved
    label = f"hunk {a.hunk + 1}" if a.hunk is not None else "the file"
    comments.prompt(tap.app, "comment", a.cid, a.rev, a.action_id, f"💬 Comment on {label} of {a.path}",
                    f"Comment on {label} (≤ 4000 chars)"[:60], reply_to=tap.message_id, path=a.path,
                    hunk=a.hunk)  # fmt: skip
    return ""


@action("review_send", arg=ReviewArg)
async def review_send(tap: Tap) -> str:
    a: ReviewArg = tap.arg
    refused = resolve.refusal(tap.app.conn, a.action_id, "comment")
    if refused or a.rev is None:
        return refused or resolve.SUPERSEDED
    comments.send_prompt(tap.app, a.cid, a.rev, a.action_id, reply_to=tap.message_id)
    return ""


@action("review_send_now", arg=ReviewArg)
async def review_send_now(tap: Tap) -> str:
    a: ReviewArg = tap.arg
    refused = resolve.refusal(tap.app.conn, a.action_id, "comment")
    if refused or a.rev is None:
        return refused or resolve.SUPERSEDED
    return comments.send(tap.app, a.cid, a.rev, a.action_id, "", actor=tap.actor)


@action("review_discard", arg=ReviewArg)
async def review_discard(tap: Tap) -> str:
    a: ReviewArg = tap.arg
    refused = resolve.refusal(tap.app.conn, a.action_id, "comment")
    if refused or a.rev is None:
        return refused or resolve.SUPERSEDED
    return comments.discard(tap.app, a.cid, a.rev, a.action_id, actor=tap.actor)


@action("comment_undo", arg=ReviewArg)
async def comment_undo(tap: Tap) -> str:
    a: ReviewArg = tap.arg
    refused = resolve.refusal(tap.app.conn, a.action_id, "comment")
    if refused or a.rev is None or a.comment_id is None:
        return refused or resolve.SUPERSEDED
    return comments.undo(tap.app, a.cid, a.rev, a.action_id, a.comment_id, actor=tap.actor)


@action("use_as_comment", arg=ReviewArg)
async def use_as_comment(tap: Tap) -> str:
    """Rule 7: converts the user's own Reviews message (its stored text, undefused), never the answer."""
    a: ReviewArg = tap.arg
    refused = resolve.refusal(tap.app.conn, a.action_id, "comment")
    if refused or a.rev is None or not a.text:
        return refused or resolve.SUPERSEDED
    return comments.save(tap.app, a.cid, a.rev, a.action_id, undefuse(a.text), actor=tap.actor)


@action("show_card", arg=ReviewArg)
async def show_card(tap: Tap) -> str:
    a: ReviewArg = tap.arg
    res = resolve.resolve(tap.app.conn, a.action_id)
    if res.submitted:
        pr = db.one(tap.app.conn, "SELECT pr_number FROM contributions WHERE id = ?", (a.cid,))
        if res.action_status == "dry_run_done":
            return "Dry run done · nothing was published"
        return f"Already submitted · PR #{pr['pr_number']}" if pr and pr["pr_number"] else "Already submitted"
    return reissue_toast(tap, a.cid)


def reissue_toast(tap: Tap, cid: str) -> str:
    try:
        ids = tap.app.reissue(cid, tap.actor, keep_expiry=True)
    except ApprovalError as exc:
        if exc.code == "submitting":
            return "Submitting… try again in a moment"
        if exc.code == "nothing_to_resend":
            state = resolve.state_of(tap.app.conn, cid)
            expired = db.one(
                tap.app.conn,
                "SELECT 1 FROM approvals a JOIN outbound_actions o ON o.id = a.action_id"
                " WHERE o.cid = ? AND a.status = 'expired' AND o.status = 'pending_approval'",
                (cid,),
            )
            if expired:
                return "Expired: tap 🔁 Retry on the card for a fresh approval."
            return resolve.nothing_text(state)
        return resolve.SUPERSEDED
    return "Re-sent the card" if len(ids) == 1 else f"Re-sent {len(ids)} cards"


@action("stale_retry", arg=ReviewArg)
async def stale_retry(tap: Tap) -> str:
    """S12 🔁 Retry: a fresh card through ``AppContext.reissue`` (eligibility applies, delta 7)."""
    a: ReviewArg = tap.arg
    if not tap.app.approvals.system_running():
        return "Paused or killed: nothing was re-sent. /resume first."
    try:
        ids = tap.app.reissue(a.cid, tap.actor, keep_expiry=True)
    except ApprovalError as exc:
        if exc.code == "submitting":
            return "Submitting… try again in a moment"
        return resolve.nothing_text(resolve.state_of(tap.app.conn, a.cid))
    return "🔁 A fresh card follows." if len(ids) == 1 else f"🔁 {len(ids)} fresh cards follow."


@action("stale_show_current", arg=StaleShowCurrentArg)
async def stale_show_current(tap: Tap) -> str:
    """Resolved by contribution (or skill), never by the stale card's action (§14.11.12, BA-11)."""
    a: StaleShowCurrentArg = tap.arg
    ctx = tap.app
    try:
        if a.skill is not None:
            ids = ctx.reissue_skill(a.skill, tap.actor, keep_expiry=True) if db.one(
                ctx.conn, "SELECT 1 FROM skill_approvals WHERE skill = ? AND status IN ('pending','armed')",
                (a.skill,)) else []  # fmt: skip
            return "Re-sent the skill card" if ids else f"No open card for skill {a.skill}"
        assert a.cid is not None
        pr_open = db.one(ctx.conn, "SELECT 1 FROM approvals a JOIN outbound_actions o ON o.id = a.action_id"
                         " WHERE o.cid = ? AND a.status IN ('pending','armed') LIMIT 1", (a.cid,))  # fmt: skip
        prop_open = db.one(ctx.conn, "SELECT 1 FROM proposal_approvals WHERE cid = ? AND status IN"
                           " ('pending','armed')", (a.cid,))  # fmt: skip
        order = ["proposal", "pr"] if a.gate == 1 else ["pr", "proposal"]
        for side in order:
            if side == "pr" and pr_open:
                ids = ctx.reissue(a.cid, tap.actor, keep_expiry=True)
                return "Re-sent the card" if len(ids) == 1 else f"Re-sent {len(ids)} cards"
            if side == "proposal" and prop_open:
                ctx.reissue_proposal(a.cid, tap.actor, keep_expiry=True)
                return "Re-sent the proposal card"
    except ApprovalError as exc:
        if exc.code == "submitting":
            return "Submitting… try again in a moment"
        return resolve.nothing_text(resolve.state_of(ctx.conn, a.cid or ""))
    return f"No open card for {a.cid} (state {resolve.state_of(ctx.conn, a.cid or '')})"


# ------------------------------------------------------------------------- typed input
@input_handler("comment")
async def on_comment(t: Typed) -> None:
    p = t.prompt
    assert p is not None
    assert p.cid is not None
    assert p.rev is not None
    refused = resolve.refusal(t.app.conn, p.action_id, "comment")
    if refused:
        _reply(t, refused)
        return
    start = end = None
    side = p.side
    if p.path and p.hunk is not None:
        files = patchview.load(t.app.gitops.derived_dir(p.cid, p.rev))
        fp = next((f for f in files if f.path == p.path), None)
        if fp is not None and p.hunk < len(fp.hunks):
            start, end = fp.hunks[p.hunk].new_range()
            side = side or "new"
    text = comments.save(t.app, p.cid, p.rev, p.action_id, t.text, path=p.path, side=side, line_start=start,
                         line_end=end, actor=t.actor)  # fmt: skip
    _reply(t, text)


@input_handler("changes")
async def on_changes(t: Typed) -> None:
    p = t.prompt
    assert p is not None
    assert p.cid is not None
    assert p.rev is not None
    _reply(t, comments.send(t.app, p.cid, p.rev, p.action_id, t.text, actor=t.actor))


@input_handler("review_send_text")
async def on_send_text(t: Typed) -> None:
    p = t.prompt
    assert p is not None
    assert p.cid is not None
    assert p.rev is not None
    _reply(t, comments.send(t.app, p.cid, p.rev, p.action_id, t.text, actor=t.actor))


@input_handler("reject_other")
async def on_reject_other(t: Typed) -> None:
    p = t.prompt
    assert p is not None
    reason = t.text.strip()[:500]
    with db.transaction(t.app.conn):
        t.app.audit.append(t.actor, "rejection_reason", {"approval_id": p.approval_id, "reason": reason}, p.cid)
    _reply(t, "Noted: the reason is in the audit log.")


def _reply(t: Typed, text: str) -> None:
    if text:
        ui_send.deliver(t.app, k.plain("fyi", (k.Const(text),)), thread_id=t.thread_id, reply_to=t.message_id)


@view_reply("review")
async def on_view_reply(t: Typed) -> None:
    """Rule 5: a reply to a card, explorer, file, interdiff, body part or commit message (§14.11.5)."""
    view = t.view
    assert view is not None
    ctx = t.app
    action_id = view["action_id"]
    res = resolve.resolve(ctx.conn, action_id)
    if res.kind in {"comment", "issue_claim"}:
        edit_text.offer_replace(t, str(view["cid"]), action_id)
        return
    if not res.open:
        if res.submitted:
            _reply(t, resolve.DRY_RUN_DONE if res.action_status == "dry_run_done" else resolve.SUBMITTED)
        else:
            _reply(t, "This card is no longer current; review the new card.")
        return
    if view["stale"]:
        _reply(t, resolve.SUPERSEDED)
        return
    cid, rev = str(view["cid"]), view["rev"]
    if not isinstance(rev, int):
        _reply(t, resolve.SUPERSEDED)
        return
    if view["kind"] == "file" and t.quote and t.quote_manual:
        state = json.loads(view["state_json"] or "{}")
        lines = tuple(tuple(x) for x in state.get("lines", []))
        hits = patchview.locate(lines, undefuse(t.quote))
        if len(hits) == 1:
            side, start, end = hits[0]
            _reply(t, comments.save(ctx, cid, rev, action_id, t.text, path=view["path"], side=side, line_start=start,
                                    line_end=end, quote=t.quote, actor=t.actor))  # fmt: skip
            return
        if len(hits) > 1:
            comments.line_picker(ctx, cid, rev, action_id, view["path"], hits, t.text, reply_to=t.message_id)
            return
        _reply(t, comments.save(ctx, cid, rev, action_id, t.text, path=view["path"], quote=t.quote, actor=t.actor))
        return
    path = view["path"] if view["kind"] in {"file", "interdiff"} else None
    if view["kind"] == "interdiff" and path is None and t.quote:  # range-diff: the file the quote sits in
        path = interdiff_view.path_for_quote(ctx, cid, rev, undefuse(t.quote))
    _reply(t, comments.save(ctx, cid, rev, action_id, t.text, path=path, quote=t.quote, actor=t.actor))
