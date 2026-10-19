"""S4: comments and the draft review (DESIGN §14.11.5; AC 7, 8, 30, 32).

Draft comments are keyed by ``(cid, rev)``, never by approval, so a Cancel, ``reissue()`` or ``/approve``
never orphans them. Limits at save time: ≤ 30 open comments per draft, ≤ 4,000 characters each; at
Send the deterministic ``change_requests.text`` is ≤ 60,000 characters. Sending creates exactly one
``change_requests`` row with ``comments_json``, moves the contribution to ``CHANGES_REQUESTED`` (human)
and invalidates its approvals (``changes_requested``), so the card turns into S5.

While the card's approval is ``armed`` the Confirm panel's "⚠️ N unsent comments" line is refreshed
in place with the same tokens (no mint, no rotate); if the approval left ``armed`` meanwhile (a disarm
or a reload raced the edit), the approval's real state is re-rendered (S1 or S12) with a
per-comment dedupe key (BA-3).
"""

from __future__ import annotations

import json
from typing import Any

from ossbot.common.clock import to_iso
from ossbot.common.ids import new_token
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import normalize_text
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.state_machine import Actor, TransitionError
from ossbot.gatekeeper.telegram import keyboards
from ossbot.gatekeeper.telegram.escape import make_visible
from ossbot.gatekeeper.telegram.review import confirm as confirm_panel
from ossbot.gatekeeper.telegram.review import resolve
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send

MAX_COMMENTS = 30
COMMENT_MAX = 4000
CHANGE_TEXT_MAX = 60_000
QUOTE_MAX = 1024
LINE_PICKER_MAX = 6
COMMENT_PLACEHOLDER = "Your comment (≤ 4000 chars)"
CHANGES_PLACEHOLDER = "Describe the change (≤ 4000 chars)"
SEND_PLACEHOLDER = "Anything to add? (optional)"
FULL = "Draft review is full (30); send or discard first"


def open_comments(ctx: AppContext, cid: str, rev: int) -> list[Any]:
    """Insertion order (rowid breaks a timestamp tie; ids are random)."""
    return list(ctx.conn.execute(
        "SELECT * FROM review_comments WHERE cid = ? AND rev = ? AND change_request_id IS NULL AND discarded_at IS"
        " NULL ORDER BY created_at, rowid", (cid, rev)).fetchall())  # fmt: skip


def where(row: Any) -> str:
    if not row["path"]:
        return "general"
    name = str(row["path"]).rsplit("/", 1)[-1]
    if row["line_start"] is None:
        return f"{name} (file)"
    end = row["line_end"] or row["line_start"]
    return f"{name} L{row['line_start']}" + (f"–{end}" if end != row["line_start"] else "")


def prompt(ctx: AppContext, kind: str, cid: str, rev: int, action_id: str | None, text: str, placeholder: str, *,
           reply_to: int | None = None, path: str | None = None, hunk: int | None = None,
           lines: list[list[Any]] | None = None) -> None:  # fmt: skip
    """A ForceReply prompt; ``pending_input`` is opened when it is sent (``Outbox.mark_sent``). ``lines``:
    one ``[side, start, end]`` already resolved (an interdiff hunk), stored as the prompt's candidate."""
    view = k.plain("confirm_prompt", (k.Text(text, 300),))
    view.placeholder = placeholder[:60]
    view.cid, view.rev, view.action_id = cid, rev, action_id
    prompt_state: dict[str, Any] = {"kind": kind, "cid": cid, "rev": rev, "action_id": action_id, "path": path,
                                    "hunk": hunk}  # fmt: skip
    if lines:
        prompt_state["line_candidates"] = lines
    view.state = {"prompt": prompt_state}
    ui_send.deliver(ctx, view, thread="reviews", reply_to=reply_to)


def save(ctx: AppContext, cid: str, rev: int, action_id: str | None, text: str, *, path: str | None = None,
         side: str | None = None, line_start: int | None = None, line_end: int | None = None,
         quote: str | None = None, actor: str) -> str:  # fmt: skip
    """Store one draft comment (limits first; nothing stored on a refusal). Returns the reply text."""
    body = normalize_text(text).strip()
    if not body:
        return "Empty comment: nothing saved."
    if len(body) > COMMENT_MAX:
        return f"Comment is {len(body):,} characters; the limit is {COMMENT_MAX:,}. Nothing saved."
    if len(open_comments(ctx, cid, rev)) >= MAX_COMMENTS:
        return FULL
    res = resolve.resolve(ctx.conn, action_id)
    comment_id = f"m{new_token()[:12]}"
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO review_comments (id, cid, rev, approval_id, source, path, side, line_start, line_end, quote,"
            " text, created_at) VALUES (?,?,?,?, 'telegram', ?,?,?,?,?,?,?)",
            (comment_id, cid, rev, res.approval_id, path, side if path and line_start else None, line_start,
             line_end, (quote or "")[:QUOTE_MAX] or None, body, to_iso(ctx.clock.now())),
        )  # fmt: skip
        ctx.audit.append(actor, "review_comment_saved", {"comment_id": comment_id, "rev": rev}, cid)
    n = len(open_comments(ctx, cid, rev))
    row = db.one(ctx.conn, "SELECT * FROM review_comments WHERE id = ?", (comment_id,))
    undo = k.button("↩️ Undo", "comment_undo", {"cid": cid, "rev": rev, "action_id": action_id,
                                                "comment_id": comment_id})  # fmt: skip
    ack = k.plain("fyi", (k.Const(f"📝 Comment {n} saved · "), k.Text(where(row), 80)), rows=[[undo]])
    ack.cid, ack.rev, ack.action_id = cid, rev, action_id
    ui_send.deliver(ctx, ack, thread="reviews", silent=True)
    refresh_draft(ctx, cid, rev, action_id)
    if res.approval_status == "armed" and res.approval_id is not None:
        refresh_confirm(ctx, res.approval_id, comment_id)
        return "Confirm pending — comments will apply if you cancel."
    return ""


def draft_view(ctx: AppContext, cid: str, rev: int, action_id: str | None, *, show_all: bool = False) -> k.View:
    rows = open_comments(ctx, cid, rev)
    lines: list[k.Line] = [(k.Const("📝 "), k.Bold(k.Const("Draft review")),
                            k.Const(f" · {cid} r{rev} · {len(rows)} comment{'s' * (len(rows) != 1)}"))]  # fmt: skip
    shown = rows if show_all else rows[:10]
    width = 70 if show_all else 120
    items: list[k.Line] = [(k.Const(f"{i}. "), k.Text(where(r), 40), k.Const(" — "), k.Text(str(r["text"]), width))
                           for i, r in enumerate(shown, 1)]  # fmt: skip
    # the header stays; comment lines go in groups of 5 that the budget rule drops from the end (AC 1, AC 53)
    blocks = [k.Block((k.Lines(tuple(lines)),), name="head")]
    blocks += [k.Block((k.Lines(tuple(items[i : i + 5])),), required=False, priority=100 - i, name=f"c{i}")
               for i in range(0, len(items), 5)]  # fmt: skip
    if len(rows) > len(shown):
        blocks.append(k.Block((k.Lines((k.line(f"+{len(rows) - len(shown)} more"),)),), name="more"))
    arg = {"cid": cid, "rev": rev, "action_id": action_id}
    buttons: k.Rows = []
    if rows:
        buttons = [[k.button(f"📨 Send {len(rows)} comment{'s' * (len(rows) != 1)}", "review_send", arg)],
                   [k.button("👁 Show all", "nav", {**arg, "view": "draft_all"}),
                    k.button("🗑 Discard", "review_discard", arg)]]  # fmt: skip
    view = k.View(kind="draft_review", blocks=blocks, rows=buttons, cid=cid, rev=rev, action_id=action_id)
    return view


def refresh_draft(ctx: AppContext, cid: str, rev: int, action_id: str | None) -> None:
    """One draft-review message per (cid, rev), a reply to the card, edited in place."""
    from ossbot.gatekeeper.telegram.review.actions import card_message  # noqa: PLC0415 - mutual helpers

    existing = db.one(ctx.conn, "SELECT message_id FROM tg_views WHERE kind = 'draft_review' AND cid = ? AND rev = ?"
                      " AND stale = 0 ORDER BY updated_at DESC LIMIT 1", (cid, rev))  # fmt: skip
    view = draft_view(ctx, cid, rev, action_id)
    if existing is not None:
        ui_send.edit(ctx, view, message_id=int(existing["message_id"]))
    else:
        ui_send.deliver(ctx, view, thread="reviews", reply_to=card_message(ctx, action_id), silent=True)


def refresh_confirm(ctx: AppContext, approval_id: str, comment_id: str) -> None:
    """The Confirm panel with the new unsent count, re-using its confirm/cancel tokens (AC 30)."""
    row = db.one(ctx.conn, "SELECT * FROM approvals WHERE id = ?", (approval_id,))
    if row is None or row["tg_message_id"] is None:
        return
    tokens = {str(r["purpose"]): str(r["token"]) for r in ctx.conn.execute(
        "SELECT purpose, token FROM ui_tokens WHERE approval_id = ? AND purpose IN ('confirm','cancel')"
        " AND used_at IS NULL", (approval_id,))}  # fmt: skip
    binding = json.loads(row["binding_json"])
    sentence = ctx.approvals.confirm_text(binding)
    if row["status"] == "armed" and {"confirm", "cancel"} <= set(tokens):
        from ossbot.gatekeeper.telegram import cards  # noqa: PLC0415 - small

        action = db.one(ctx.conn, "SELECT id, cid, kind, payload_json FROM outbound_actions WHERE id = ?",
                        (row["action_id"],))  # fmt: skip
        assert action is not None
        rev = json.loads(action["payload_json"]).get("rev")
        text = confirm_panel.panel(
            ctx.approvals.card_view(approval_id).card, kind=str(action["kind"]), live=not binding["dry_run"],
            sentence=sentence, cid=str(action["cid"]), rev=rev if isinstance(rev, int) else None,
            files=cards.file_count(ctx.conn, str(action["id"])), conn=ctx.conn, now=ctx.clock.now(),
            confirm_ttl_s=ctx.config.policy.limits.confirm_ttl_seconds,
        )  # fmt: skip
        markup = keyboards.to_json(keyboards.confirm_rows(tokens["confirm"], tokens["cancel"],
                                                          live=not binding["dry_run"]))  # fmt: skip
        ctx.outbox.enqueue("editMessageText", {"chat_id": row["tg_chat_id"], "message_id": row["tg_message_id"],
                                               "text": text, "parse_mode": "HTML", "reply_markup": markup},
                           dedupe_key=f"view:{new_token()}", thread_key="reviews")  # fmt: skip
    after = db.one(ctx.conn, "SELECT status FROM approvals WHERE id = ?", (approval_id,))
    if after is not None and after["status"] != "armed":  # a disarm or a reload raced the refresh
        from ossbot.gatekeeper.telegram.publisher import CardPublisher  # noqa: PLC0415 - avoids a cycle

        CardPublisher(ctx).render_current(approval_id, comment_id)


def send_prompt(ctx: AppContext, cid: str, rev: int, action_id: str | None, *, reply_to: int | None) -> None:
    """📨 Send: a ForceReply "Anything to add?" with a ``[📨 Send now]`` button on the same message."""
    view = k.plain("confirm_prompt", k.line("Anything to add? Reply, or tap Send now"),
                   rows=[[k.button("📨 Send now", "review_send_now", {"cid": cid, "rev": rev, "action_id": action_id},
                                   single_use=True)]])  # fmt: skip
    view.placeholder = SEND_PLACEHOLDER
    view.cid, view.rev, view.action_id = cid, rev, action_id
    view.state = {"prompt": {"kind": "review_send_text", "cid": cid, "rev": rev, "action_id": action_id}}
    ui_send.deliver(ctx, view, thread="reviews", reply_to=reply_to)


def change_text(free_text: str, rows: list[Any]) -> str:
    """Deterministic: the optional free text, then numbered comments with their quoted lines."""
    parts: list[str] = []
    if free_text.strip():
        parts.append(normalize_text(free_text).strip())
    for i, r in enumerate(rows, 1):
        loc = r["path"] or "general"
        if r["line_start"] is not None:
            end = r["line_end"] or r["line_start"]
            loc += f":{r['line_start']}" + (f"-{end}" if end != r["line_start"] else "")
        parts.append(f"{i}. {loc} — {r['text']}")
        if r["quote"]:
            parts.append("```\n" + str(r["quote"]) + "\n```")
    return "\n\n".join(parts)


def send(ctx: AppContext, cid: str, rev: int, action_id: str | None, free_text: str, *, actor: str) -> str:
    """One ``change_requests`` row (text + comments_json), CHANGES_REQUESTED, approvals invalidated."""
    refused = resolve.refusal(ctx.conn, action_id, "comment")
    if refused:
        return refused
    res = resolve.resolve(ctx.conn, action_id)
    if res.approval_status == "armed":
        return "Cancel the Confirm first: a pending Confirm can't take a change request."
    rows = open_comments(ctx, cid, rev)
    text = change_text(free_text, rows)
    if not text.strip():
        return "Nothing to send: write the change or add comments first."
    if len(text) > CHANGE_TEXT_MAX:
        return (f"Review is {len(text):,} characters; the limit is {CHANGE_TEXT_MAX:,}. Remove or shorten comments"
                " first")  # fmt: skip
    request_id = f"r_{new_token()[:12]}"
    structured = [{"path": r["path"], "side": r["side"], "line_start": r["line_start"], "line_end": r["line_end"],
                   "quote": r["quote"], "text": r["text"], "source": r["source"]} for r in rows]  # fmt: skip
    now = to_iso(ctx.clock.now())
    try:
        with db.transaction(ctx.conn):
            contrib = db.one(ctx.conn, "SELECT state FROM contributions WHERE id = ?", (cid,))
            if contrib is None or contrib["state"] != State.AWAITING_APPROVAL.value:
                return resolve.nothing_text(str(contrib["state"]) if contrib else "unknown")
            ctx.conn.execute(
                "INSERT INTO change_requests (id, cid, text, created_at, comments_json) VALUES (?,?,?,?,?)",
                (request_id, cid, text, now, json.dumps(structured, ensure_ascii=False)),
            )
            ctx.conn.execute("UPDATE review_comments SET change_request_id = ? WHERE cid = ? AND rev = ? AND"
                             " change_request_id IS NULL AND discarded_at IS NULL", (request_id, cid, rev))  # fmt: skip
            ctx.approvals.invalidate_contribution(cid, "changes_requested", actor)
            ctx.conn.execute("UPDATE outbound_actions SET status = 'cancelled' WHERE cid = ? AND kind IN"
                             " ('open_pr','push_update') AND status = 'pending_approval'", (cid,))  # fmt: skip
            ctx.state_machine.transition(cid, State.AWAITING_APPROVAL, State.CHANGES_REQUESTED, Actor.HUMAN,
                                         {"change_request": request_id, "comments": len(rows)}, actor)  # fmt: skip
    except TransitionError as exc:
        return f"Not sent: {exc.code}"
    refresh_draft(ctx, cid, rev, action_id)
    return f"✏️ Change request sent ({len(rows)} comment{'s' * (len(rows) != 1)}); r{rev + 1} starts."


def discard(ctx: AppContext, cid: str, rev: int, action_id: str | None, *, actor: str) -> str:
    with db.transaction(ctx.conn):
        cur = ctx.conn.execute("UPDATE review_comments SET discarded_at = ? WHERE cid = ? AND rev = ? AND"
                               " change_request_id IS NULL AND discarded_at IS NULL",
                               (to_iso(ctx.clock.now()), cid, rev))  # fmt: skip
        ctx.audit.append(actor, "review_discarded", {"rev": rev, "comments": cur.rowcount}, cid)
    refresh_draft(ctx, cid, rev, action_id)
    return f"🗑 Discarded {cur.rowcount} comment{'s' * (cur.rowcount != 1)}."


def undo(ctx: AppContext, cid: str, rev: int, action_id: str | None, comment_id: str, *, actor: str) -> str:
    with db.transaction(ctx.conn):
        cur = ctx.conn.execute("UPDATE review_comments SET discarded_at = ? WHERE id = ? AND cid = ? AND"
                               " change_request_id IS NULL AND discarded_at IS NULL",
                               (to_iso(ctx.clock.now()), comment_id, cid))  # fmt: skip
        if cur.rowcount:
            ctx.audit.append(actor, "review_comment_removed", {"comment_id": comment_id}, cid)
    if cur.rowcount == 0:
        return "Already sent or removed."
    refresh_draft(ctx, cid, rev, action_id)
    return "↩️ Comment removed."


def line_picker(ctx: AppContext, cid: str, rev: int, action_id: str | None, path: str | None,
                hits: list[tuple[str, int, int]], text: str, *, reply_to: int) -> None:  # fmt: skip
    """An ambiguous quote: ≤ 6 candidate lines; the comment text rides in each button's arg."""
    body = normalize_text(text).strip()[:COMMENT_MAX]
    buttons = [k.button(f"L{start}" + (f"–{end}" if end != start else ""), "comment_line_pick",
                        {"cid": cid, "rev": rev, "action_id": action_id, "path": path, "side": side, "line": start,
                         "text": body}, single_use=True)
               for side, start, end in hits[:LINE_PICKER_MAX]]  # fmt: skip
    head = (k.Const("The quote matches several places; pick the line for: "), k.Text(make_visible(body)[0], 80))
    view = k.plain("picker", head, rows=[buttons[i : i + 3] for i in range(0, len(buttons), 3)])
    view.cid, view.rev, view.action_id = cid, rev, action_id
    ui_send.deliver(ctx, view, thread="reviews", reply_to=reply_to)
