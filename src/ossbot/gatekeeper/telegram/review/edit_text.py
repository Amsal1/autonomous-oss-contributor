"""S7: edit the PR title or body, or a comment/claim body (DESIGN §14.11.8, §14.10.4 rule 5; AC 60).

* ``📝 Edit text`` on an ``open_pr`` card sends a picker (a reply to the card): Title, Body, Body as
  file, Copy title, and Clear my edits when an override exists. On ``comment``/``issue_claim`` it opens
  the ``edit_comment`` ForceReply directly.
* Every prompt quotes the current publish text verbatim (scanned, never redacted or defused, U14).
* Every accepted edit passes ``policy.check_text`` with the gatekeeper's secret values **before**
  anything is stored (P1-4, BA-21), invalidates the approval (``text_edited``) and re-sends the card
  through ``AppContext.reissue`` with its expiry kept; the old card becomes S12.
* A reply to a comment/claim card offers ``Replace the comment text with your reply?`` (rule 5); Replace
  runs this module's ``edit_comment`` (BA-13), never a copy of it.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ossbot.common.schemas import State
from ossbot.gatekeeper import db, policy
from ossbot.gatekeeper.approvals import ApprovalError, normalize_text, reissue_eligible
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.actions import Tap, action
from ossbot.gatekeeper.telegram.inputs import Typed, input_handler
from ossbot.gatekeeper.telegram.review import resolve
from ossbot.gatekeeper.telegram.review.resolve import CID_PATTERN, ReviewArg
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send
from ossbot.gatekeeper.telegram.ui.sanitize import undefuse

MARKER = "<!-- ossbot:action="
TITLE_PLACEHOLDER = "New title (1-256 chars, one line)"
BODY_PLACEHOLDER = "New PR body (≤ 60,000 chars)"
COMMENT_PLACEHOLDER = "New comment text (≤ 4,000 chars)"
FILE_PLACEHOLDER = "Reply with a .md or .txt document"
REPLACE_TTL_MIN = 15
QUOTE_MAX = 3000
BODY_FILE_MAX = 64 * 1024


class ReplaceArg(BaseModel):
    """BA-13: ``{cid, action_id, text}``; text 1-4,000 characters, undefused and normalized."""

    model_config = ConfigDict(extra="forbid")
    cid: str = Field(pattern=CID_PATTERN)
    action_id: str = Field(pattern=r"^x_[0-9A-HJKMNP-TV-Z]{10}$")
    text: str = Field(min_length=1, max_length=4000)


def _contrib(ctx: AppContext, cid: str) -> Any:
    return db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))


def open_picker(ctx: AppContext, cid: str, rev: int, action_id: str | None, kind: str, *, card_message: int) -> None:
    if kind in {"comment", "issue_claim"}:
        prompt_comment(ctx, cid, action_id, reply_to=card_message)
        return
    c = _contrib(ctx, cid)
    title = (c["pr_title_override"] if c["pr_title_override"] is not None else c["pr_title"]) or ""
    arg = {"cid": cid, "rev": rev, "action_id": action_id}
    rows: k.Rows = [[k.button("✏️ Title", "edit_title", arg), k.button("✏️ Body", "edit_body", arg)],
                    [k.button("📄 Body as file", "body_file", arg), k.copy_button("📋 Copy title", title)]]  # fmt: skip
    if c["pr_title_override"] is not None or c["pr_body_override"] is not None:
        rows.append([k.button("↩️ Clear my edits", "clear_edits", arg, single_use=True)])
    lines = (
        k.line(f"📝 Edit the PR text of {cid} r{rev}"),
        (k.Const("Title: "), k.Code(k.Verbatim(title, "pr_title"))),
    )
    blocks = [k.Block((k.Lines(lines),))]
    view = k.View(kind="picker", blocks=blocks, rows=rows, cid=cid, rev=rev, action_id=action_id)
    ui_send.deliver(ctx, view, thread="reviews", reply_to=card_message)


def _prompt(ctx: AppContext, kind: str, text: list[k.Line], verbatim: str, placeholder: str, *, cid: str,
            rev: int | None, action_id: str | None, reply_to: int | None) -> None:  # fmt: skip
    shown = verbatim if len(verbatim) <= QUOTE_MAX else verbatim[:QUOTE_MAX]
    blocks = [k.Block((k.Lines(tuple(text)),)), k.Block((k.PreBlock(shown, verbatim=True),), name="current")]
    if len(verbatim) > QUOTE_MAX:
        blocks.append(k.Block((k.Lines((k.line(f"… ({len(verbatim):,} characters; the full text is in the parts"
                                               " under the card)"),)),)))  # fmt: skip
    view = k.View(kind="confirm_prompt", blocks=blocks, cid=cid, rev=rev, action_id=action_id,
                  placeholder=placeholder)  # fmt: skip
    view.state = {"prompt": {"kind": kind, "cid": cid, "rev": rev, "action_id": action_id}}
    ui_send.deliver(ctx, view, thread="reviews", reply_to=reply_to)


def prompt_comment(ctx: AppContext, cid: str, action_id: str | None, *, reply_to: int | None) -> None:
    row = db.one(ctx.conn, "SELECT payload_json FROM outbound_actions WHERE id = ?", (action_id,))
    body = str(json.loads(row["payload_json"]).get("body", "")) if row else ""
    shown = body.split(MARKER, 1)[0].rstrip()
    _prompt(ctx, "edit_comment", [k.line(f"📝 Reply with the new text of the comment ({cid}). Current text:")], shown,
            COMMENT_PLACEHOLDER, cid=cid, rev=None, action_id=action_id, reply_to=reply_to)  # fmt: skip


# --------------------------------------------------------------------------- the effects
def _resend(ctx: AppContext, cid: str, actor: str) -> str:
    try:
        ids = ctx.reissue(cid, actor, keep_expiry=True)
    except ApprovalError as exc:
        return "Saved; " + ("submitting… the card follows shortly" if exc.code == "submitting"
                            else resolve.nothing_text(resolve.state_of(ctx.conn, cid)).lower())  # fmt: skip
    return "✏️ Saved: a new card follows (approve it again)." if ids else "✏️ Saved."


def edit_pr_text(ctx: AppContext, cid: str, *, title: str | None = None, body: str | None = None,
                 actor: str) -> str:  # fmt: skip
    if title is not None and ("\n" in title.strip() or not 1 <= len(title.strip()) <= 256):
        return "Not saved: the title must be one line of 1-256 characters."
    try:
        ctx.approvals.set_text_override(cid, title=title.strip() if title is not None else None, body=body,
                                        actor=actor)  # fmt: skip
    except ApprovalError as exc:
        return f"Not saved: {str(exc)[:200]}"
    return _resend(ctx, cid, actor)


def edit_comment(ctx: AppContext, cid: str, action_id: str, text: str, *, actor: str) -> str:
    """The S7 ``edit_comment`` path (also Replace, BA-13): normalized, policy-checked, then stored."""
    with db.transaction(ctx.conn):
        action = db.one(ctx.conn, "SELECT * FROM outbound_actions WHERE id = ? AND cid = ?", (action_id, cid))
        contrib = _contrib(ctx, cid)
        if action is None or contrib is None or action["kind"] not in {"comment", "issue_claim"}:
            return resolve.SUPERSEDED
        try:
            eligible = reissue_eligible(action["kind"], action["status"], State(contrib["state"]))
        except ValueError:
            eligible = False
        if not eligible:
            return resolve.SUBMITTED if action["status"] in resolve.SUBMITTED_STATUSES else resolve.SUPERSEDED
        clean = normalize_text(undefuse(text)).strip()
        if not clean or len(clean) > 4000:
            return "Not saved: the comment must be 1-4,000 characters."
        body = clean + f"\n\n{MARKER}{action_id} -->"
        violations = policy.check_text(
            "comment", body, ctx.config, policy.effective_confidentiality(ctx.conn, ctx.config), ctx.kiro_key_sha256,
            secret_values=ctx.secret_scanner.values,
        )  # fmt: skip
        if violations:
            return "Not saved: " + ", ".join(sorted({v.code for v in violations}))
        payload = json.loads(action["payload_json"])
        payload["body"] = body
        ctx.conn.execute("UPDATE outbound_actions SET payload_json = ? WHERE id = ?",
                         (json.dumps(payload, sort_keys=True), action_id))  # fmt: skip
        ctx.audit.append(actor, "comment_text_edited", {"action_id": action_id}, cid)
        ctx.approvals._invalidate("a.status IN ('pending','armed') AND a.action_id = ?", (action_id,), "text_edited",
                                  actor)  # fmt: skip
    return _resend(ctx, cid, actor)


def offer_replace(t: Typed, cid: str, action_id: str | None) -> None:
    """Rule 5 for comment/claim cards: a confirm panel, nothing stored until Replace."""
    res = resolve.resolve(t.app.conn, action_id)
    if not res.open:
        text = (resolve.DRY_RUN_DONE if res.action_status == "dry_run_done" else resolve.SUBMITTED) if res.submitted \
            else "This card is no longer current; review the new card."  # fmt: skip
        ui_send.deliver(t.app, k.plain("fyi", k.line(text)), thread_id=t.thread_id, reply_to=t.message_id)
        return
    clean = normalize_text(undefuse(t.text)).strip()[:4000]
    if not clean or action_id is None:
        return
    arg = {"cid": cid, "action_id": action_id, "text": clean}
    rows = [[k.button("✅ Replace", "comment_replace", arg, single_use=True, ttl=_ttl()),
             k.button("↩️ Cancel", "comment_replace_cancel", {"cid": cid, "action_id": action_id}, single_use=True,
                      ttl=_ttl())]]  # fmt: skip
    view = k.plain("confirm_prompt", k.line(f"Replace the comment text with your reply? · {cid}"), rows=rows)
    ui_send.deliver(t.app, view, thread_id=t.thread_id, reply_to=t.message_id)


def _ttl() -> timedelta:
    return timedelta(minutes=REPLACE_TTL_MIN)


class CancelArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cid: str = Field(pattern=CID_PATTERN)
    action_id: str = Field(pattern=r"^x_[0-9A-HJKMNP-TV-Z]{10}$")


# --------------------------------------------------------------------------- buttons
@action("edit_title", arg=ReviewArg)
async def edit_title(tap: Tap) -> str:
    a: ReviewArg = tap.arg
    if resolve.refusal(tap.app.conn, a.action_id, "comment"):
        return resolve.refusal(tap.app.conn, a.action_id, "comment") or ""
    c = _contrib(tap.app, a.cid)
    title = (c["pr_title_override"] if c["pr_title_override"] is not None else c["pr_title"]) or ""
    _prompt(tap.app, "edit_title", [k.line(f"✏️ Reply with the new PR title for {a.cid}. Current title:")], title,
            TITLE_PLACEHOLDER, cid=a.cid, rev=a.rev, action_id=a.action_id, reply_to=tap.message_id)  # fmt: skip
    return ""


@action("edit_body", arg=ReviewArg)
async def edit_body(tap: Tap) -> str:
    a: ReviewArg = tap.arg
    if resolve.refusal(tap.app.conn, a.action_id, "comment"):
        return resolve.refusal(tap.app.conn, a.action_id, "comment") or ""
    c = _contrib(tap.app, a.cid)
    body = (c["pr_body_override"] if c["pr_body_override"] is not None else c["pr_body"]) or ""
    _prompt(tap.app, "edit_body", [k.line(f"✏️ Reply with the new PR body for {a.cid}. Current body:")], body,
            BODY_PLACEHOLDER, cid=a.cid, rev=a.rev, action_id=a.action_id, reply_to=tap.message_id)  # fmt: skip
    return ""


@action("body_file", arg=ReviewArg)
async def body_file(tap: Tap) -> str:
    a: ReviewArg = tap.arg
    if resolve.refusal(tap.app.conn, a.action_id, "comment"):
        return resolve.refusal(tap.app.conn, a.action_id, "comment") or ""
    view = k.plain("confirm_prompt", k.line(f"📄 Reply with the new PR body of {a.cid} as a .md or .txt document"
                                            " (≤ 64 KiB, UTF-8)."))  # fmt: skip
    view.placeholder = FILE_PLACEHOLDER
    view.cid, view.rev, view.action_id = a.cid, a.rev, a.action_id
    view.state = {"prompt": {"kind": "body_file", "cid": a.cid, "rev": a.rev, "action_id": a.action_id}}
    ui_send.deliver(tap.app, view, thread="reviews", reply_to=tap.message_id)
    return ""


@action("clear_edits", arg=ReviewArg)
async def clear_edits(tap: Tap) -> str:
    a: ReviewArg = tap.arg
    if resolve.refusal(tap.app.conn, a.action_id, "comment"):
        return resolve.refusal(tap.app.conn, a.action_id, "comment") or ""
    with db.transaction(tap.app.conn):
        tap.app.conn.execute("UPDATE contributions SET pr_title_override = NULL, pr_body_override = NULL WHERE id = ?",
                             (a.cid,))  # fmt: skip
        tap.app.audit.append(tap.actor, "pr_text_cleared", {}, a.cid)
        tap.app.approvals.invalidate_contribution(a.cid, "text_edited", tap.actor)
    return _resend(tap.app, a.cid, tap.actor)


@action("comment_replace", arg=ReplaceArg)
async def comment_replace(tap: Tap) -> str:
    a: ReplaceArg = tap.arg
    out = edit_comment(tap.app, a.cid, a.action_id, a.text, actor=tap.actor)
    ui_send.edit(tap.app, k.plain("fyi", k.line(out[:300])), message_id=tap.message_id)
    return ""


@action("comment_replace_cancel", arg=CancelArg)
async def comment_replace_cancel(tap: Tap) -> str:
    ui_send.edit(tap.app, k.plain("fyi", k.line("Not changed.")), message_id=tap.message_id)
    return ""


# --------------------------------------------------------------------------- replies
def _done(t: Typed, text: str) -> None:
    ui_send.deliver(t.app, k.plain("fyi", (k.Const(text[:500]),)), thread_id=t.thread_id, reply_to=t.message_id)


@input_handler("edit_title")
async def on_title(t: Typed) -> None:
    assert t.prompt is not None
    assert t.prompt.cid is not None
    _done(t, edit_pr_text(t.app, t.prompt.cid, title=t.text, actor=t.actor))


@input_handler("edit_body")
async def on_body(t: Typed) -> None:
    assert t.prompt is not None
    assert t.prompt.cid is not None
    _done(t, edit_pr_text(t.app, t.prompt.cid, body=t.text, actor=t.actor))


@input_handler("edit_comment")
async def on_comment_text(t: Typed) -> None:
    assert t.prompt is not None
    assert t.prompt.cid is not None
    if t.prompt.action_id is None:
        return
    _done(t, edit_comment(t.app, t.prompt.cid, t.prompt.action_id, t.text, actor=t.actor))


def check_body_file(name: str | None, mime: str | None, data: bytes) -> str:
    """Body-as-file validation (§14.20.6): ≤ 64 KiB, .md/.txt or a text MIME type, strict UTF-8."""
    if len(data) > BODY_FILE_MAX:
        raise ValueError(f"the file is {len(data):,} bytes; the limit is 64 KiB")
    ok_type = (mime or "") in {"text/markdown", "text/plain", "text/x-markdown"} or (name or "").lower().endswith(
        (".md", ".txt"))  # fmt: skip
    if not ok_type:
        raise ValueError("send a .md or .txt document")
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("the file is not UTF-8") from exc


async def fetch_document(application: Any, file_id: str, limit: int) -> bytes:
    """``getFile`` + download through the proxy, capped on the reported and the actual size (call only
    from a spawned task, §14.18.5; BA-2 keeps this out of handlers.py)."""
    if application is None:
        raise ValueError("telegram is not connected")
    tg_file = await application.bot.get_file(file_id)
    if (tg_file.file_size or 0) > limit:
        raise ValueError(f"the file is {tg_file.file_size:,} bytes; the limit is {limit:,}")
    data = bytes(await tg_file.download_as_bytearray())
    if len(data) > limit:
        raise ValueError(f"the file is {len(data):,} bytes; the limit is {limit:,}")
    return data


@input_handler("body_file")
async def on_body_file(t: Typed) -> None:
    assert t.prompt is not None
    assert t.prompt.cid is not None
    if t.document is None:
        _done(t, "Not saved: reply with a document (.md or .txt).")
        return
    file_id, mime, size, name = t.document
    if size > BODY_FILE_MAX:
        _done(t, f"Not saved: the file is {size:,} bytes; the limit is 64 KiB.")
        return
    cid = t.prompt.cid

    async def download() -> None:  # getFile is I/O: a spawned task (§14.18.5)
        try:
            data = await fetch_document(t.handlers.application, file_id, BODY_FILE_MAX)
            text = check_body_file(name, mime, data)
        except ValueError as exc:
            _done(t, f"Not saved: {exc}")
            return
        _done(t, edit_pr_text(t.app, cid, body=text, actor=t.actor))

    t.handlers.spawn(download(), "media")
