"""Views -> outbox rows ("mint then bind", DESIGN §14.18.2).

``deliver`` renders a ``View`` (rich when allowed, always with a classic fallback), mints its button
tokens with ``tg_message_id = NULL`` and ``approval_id = NULL`` (delta 1; ``cid`` and the purpose's
``arg`` carry the context), and writes ONE outbox row carrying the payload, ``bind_tokens_json`` and
``view_json``, all in one transaction. ``Outbox.mark_sent`` binds the tokens and writes the
``tg_views`` row in its own transaction, so a crash can never lose a binding. ``edit`` re-renders a
view in place and mints its tokens bound to the known message id.

The view kind is validated here (``ViewKind`` mirrors the ``tg_views.kind`` CHECK, BA-6), so a bad
kind fails before anything is sent. The topic is a name (``thread``); the worker resolves it to a
``message_thread_id`` when the row is sent (``telegram.topics``).
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from ossbot.common.clock import to_iso
from ossbot.common.ids import new_token
from ossbot.gatekeeper import db
from ossbot.gatekeeper.telegram import rawapi
from ossbot.gatekeeper.telegram.prefs import UxPrefs
from ossbot.gatekeeper.telegram.ui import blocks, classic, rich
from ossbot.gatekeeper.telegram.ui.kit import ButtonSpec, View, ViewTooLarge
from ossbot.gatekeeper.telegram.ui.limits import COPY_TEXT_MAX, check_placeholder

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

log = logging.getLogger(__name__)
ViewKind = Literal[
    "dashboard", "card", "explorer", "file", "interdiff", "draft_review", "status", "digest", "feature", "picker",
    "progress", "chat_answer", "body_part", "commit_msg", "fyi", "confirm_prompt", "proposal", "proposal_details",
    "rescope", "ceiling", "work_request", "link_card", "skill_card", "skill_part",
]  # fmt: skip
Thread = Literal["control", "reviews", "feed", "assistant"]
WITHHELD = "⚠️ Withheld: it matched a secret pattern (bug) · nothing can be approved from it; see /logs"
TOO_LARGE = "⚠️ View too large to render (bug) · see /logs"


class ViewRecord(BaseModel):
    """The ``tg_views`` row ``mark_sent`` writes (§14.20.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: ViewKind
    approval_id: str | None = None
    cid: str | None = None
    rev: int | None = None
    action_id: str | None = None
    path: str | None = None
    page: int | None = None
    state: dict[str, Any] = {}


def view_record(view: View) -> dict[str, Any]:
    try:
        rec = ViewRecord.model_validate({
            "kind": view.kind, "approval_id": view.approval_id, "cid": view.cid, "rev": view.rev,
            "action_id": view.action_id, "path": view.path, "page": view.page, "state": dict(view.state),
        })  # fmt: skip
    except ValidationError as exc:
        raise ValueError(f"bad view record: {exc.errors()[0]['msg']}") from exc
    return rec.model_dump()


# ------------------------------------------------------------------------------- buttons
def _button_json(spec: ButtonSpec, token: str | None) -> dict[str, Any]:
    btn: dict[str, Any] = {"text": spec.label}
    if spec.style:
        btn["style"] = spec.style
    if spec.disabled:
        btn["disabled"] = {}
    elif spec.url is not None:
        btn["url"] = spec.url
    elif spec.copy_text is not None:
        btn["copy_text"] = {"text": spec.copy_text}  # never cut: ``keyboard`` drops an over-long one
    else:
        tok = spec.token or token
        if tok is None:
            raise ValueError(f"button {spec.label!r} has no action")
        btn["callback_data"] = f"t:{tok}"
    rawapi.InlineKeyboardButton.model_validate(btn)  # the Bot API 10.3 shape (extra="forbid")
    return btn


def mint(ctx: AppContext, view: View, *, chat_id: int, message_id: int | None) -> list[list[str | None]]:
    """One token per callback button (``approval_id`` NULL, delta 1). Caller holds the transaction."""
    now = ctx.clock.now()
    out: list[list[str | None]] = []
    for row in view.rows:
        toks: list[str | None] = []
        for spec in row:
            if spec.purpose is None or spec.token is not None or spec.disabled:
                toks.append(None)
                continue
            arg = dict(spec.arg or {})
            cid = arg.get("cid") if isinstance(arg.get("cid"), str) else view.cid
            stored = spec.raw_arg if spec.raw_arg is not None else json.dumps(arg, sort_keys=True)
            toks.append(ctx.approvals.mint_token(
                spec.purpose, chat_id=chat_id, message_id=message_id, expires_at=now + spec.ttl,
                approval_id=None, cid=cid, arg=stored, single_use=spec.single_use,
            ))  # fmt: skip
        out.append(toks)
    return out


def _copy_fits(spec: ButtonSpec) -> bool:
    """§14.11.2: ``📋 Copy title`` copies the stored text unchanged, so a text over the Bot API limit drops
    the button instead of being cut."""
    if spec.copy_text is None or len(spec.copy_text) <= COPY_TEXT_MAX:
        return True
    log.warning("copy button %r omitted: %d > %d characters", spec.label, len(spec.copy_text), COPY_TEXT_MAX,
                extra={"event": "copy_text_too_long"})  # fmt: skip
    return False


def keyboard(view: View, tokens: list[list[str | None]]) -> dict[str, Any] | None:
    if not view.rows:
        return None
    rows = [[_button_json(s, t) for s, t in zip(row, toks, strict=True) if _copy_fits(s)]
            for row, toks in zip(view.rows, tokens, strict=True)]  # fmt: skip
    return {"inline_keyboard": [r for r in rows if r]}


# ------------------------------------------------------------------------------- payloads
def _classic_payload(ctx: AppContext, view: View, chat_id: int) -> tuple[dict[str, Any], int]:
    try:
        out = classic.render(view)
    except ViewTooLarge as exc:
        log.error("view too large: %s %s", exc.kind, exc.sizes, extra={"event": "view_too_large", "kind": exc.kind})
        return {"chat_id": chat_id, "text": TOO_LARGE, "parse_mode": "HTML"}, -1
    if out.verbatim_hits:
        return {"chat_id": chat_id, "text": WITHHELD, "parse_mode": "HTML"}, -1
    payload: dict[str, Any] = {"chat_id": chat_id, "text": out.text, "parse_mode": "HTML",
                               "link_preview_options": {"is_disabled": True}}  # fmt: skip
    return payload, 0


def build(ctx: AppContext, view: View, *, chat_id: int, tokens: list[list[str | None]],
          reply_to: int | None = None, silent: bool | None = None) -> tuple[str, dict[str, Any], bool]:  # fmt: skip
    """(method, payload, ok). ``ok`` False = the view was replaced by a fixed stub (no buttons)."""
    view.check()
    payload, status = _classic_payload(ctx, view, chat_id)
    ok = status == 0
    markup = keyboard(view, tokens) if ok else None
    if ok and view.placeholder is not None:
        placeholder = check_placeholder(view.placeholder)
        if markup is None:
            markup = rawapi.ForceReply(input_field_placeholder=placeholder).payload()
        else:
            markup["force_reply"] = True
    if markup is not None:
        payload["reply_markup"] = markup
    if reply_to is not None:
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    if silent is not None:
        payload["disable_notification"] = silent
    prefs = UxPrefs(ctx.conn, ctx.clock)
    if not ok or not rich.use_rich(view, prefs, ctx.config.telegram.rich_views_default):
        return "sendMessage", payload, ok
    try:
        message = rich.render(view)
    except ViewTooLarge:
        return "sendMessage", payload, ok
    req = rawapi.SendRichMessage.model_validate({
        "chat_id": chat_id, "rich_message": blocks.dump(message), "disable_notification": silent,
        "reply_parameters": payload.get("reply_parameters"),
        "reply_markup": markup if markup and "inline_keyboard" in markup else None,
    })  # fmt: skip
    rich_payload = req.payload()
    rich_payload["_classic"] = payload  # the worker falls back to it on a rich 400 (§14.20.5)
    return "sendRichMessage", rich_payload, ok


def deliver(
    ctx: AppContext,
    view: View,
    *,
    thread: Thread | None = None,
    thread_id: int | None = None,
    reply_to: int | None = None,
    priority: int = 2,
    dedupe_key: str | None = None,
    silent: bool | None = None,
    coalesce_key: str | None = None,
) -> int | None:
    """Queue a view; returns the outbox row id (None when deduplicated or Telegram is unset).
    ``thread`` names a system topic (resolved at send time); ``thread_id`` is the originating thread of
    a command reply (a user topic too, §14.10.1 "where replies go")."""
    chat_id = ctx.telegram_chat_id
    if chat_id == 0:
        log.warning("telegram not configured; %s view dropped", view.kind, extra={"event": "view_dropped"})
        return None
    record = view_record(view)
    with db.transaction(ctx.conn):
        tokens = mint(ctx, view, chat_id=chat_id, message_id=None)
        method, payload, ok = build(ctx, view, chat_id=chat_id, tokens=tokens, reply_to=reply_to, silent=silent)
        if thread is None and thread_id is not None:
            payload["message_thread_id"] = thread_id
            if isinstance(payload.get("_classic"), dict):
                payload["_classic"]["message_thread_id"] = thread_id
        flat = [t for row in tokens for t in row if t is not None]
        if not ok:  # stub: no buttons; the minted tokens die unbound
            for tok in flat:
                ctx.conn.execute("UPDATE ui_tokens SET used_at = ? WHERE token = ?", (to_iso(ctx.clock.now()), tok))
            flat = []
        return ctx.outbox.enqueue(
            method, payload, dedupe_key=dedupe_key or f"view:{new_token()}", bind_tokens=flat or None,
            view=record if ok else {"kind": "fyi"}, priority=priority, thread_key=thread, coalesce_key=coalesce_key,
        )  # fmt: skip


DOCUMENT_MAX = 45 * 1024 * 1024  # Bot API upload 50 MB (§14.17.4)
DOC_NAME_RE = r"^[A-Za-z0-9._-]{1,80}$"


def document(ctx: AppContext, name: str, data: bytes, *, caption: str = "", thread: Thread | None = None,
             thread_id: int | None = None, reply_to: int | None = None) -> int | None:  # fmt: skip
    """``sendDocument`` through the outbox: the bytes are spooled to the gatekeeper's state dir (the
    payload keeps only the path) and ``bot.TelegramSender`` uploads them. Callers redact or scan first."""
    chat_id = ctx.telegram_chat_id
    if chat_id == 0 or not re.match(DOC_NAME_RE, name) or len(data) > DOCUMENT_MAX:
        return None
    spool = ctx.paths.gatekeeper_dir / "tg-docs"
    spool.mkdir(parents=True, exist_ok=True)
    path = spool / f"{new_token()}-{name}"
    path.write_bytes(data)
    payload: dict[str, Any] = {"chat_id": chat_id, "_document_path": str(path), "_document_name": name}
    if caption:
        payload["caption"] = caption[:900]
        payload["parse_mode"] = "HTML"
    if thread is None and thread_id is not None:
        payload["message_thread_id"] = thread_id
    if reply_to is not None:
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    return ctx.outbox.enqueue("sendDocument", payload, dedupe_key=f"view:{new_token()}", thread_key=thread)


def edit(ctx: AppContext, view: View, *, message_id: int, dedupe_key: str | None = None,
         chat_id: int | None = None, rich_message: bool = False) -> int | None:  # fmt: skip
    """Re-render a view in place (``editMessageText``); tokens are minted bound to ``message_id``.
    A message that was sent rich is edited rich, with the classic view as its fallback (BA-9)."""
    chat = chat_id or ctx.telegram_chat_id
    if chat == 0:
        return None
    record = view_record(view)
    with db.transaction(ctx.conn):
        tokens = mint(ctx, view, chat_id=chat, message_id=message_id)
        _, payload, ok = build(ctx, view, chat_id=chat, tokens=tokens)
        payload["message_id"] = message_id
        payload.pop("disable_notification", None)
        method = "editMessageText"
        if rich_message and ok:
            message = rich.render(view)
            req = rawapi.EditMessageRich.model_validate({
                "chat_id": chat, "message_id": message_id, "rich_message": blocks.dump(message),
                "reply_markup": payload.get("reply_markup"),
            })  # fmt: skip
            classic_payload = payload
            payload = req.payload()
            payload["_classic"] = classic_payload
            payload["_edit_fallback"] = True
        return ctx.outbox.enqueue(method, payload, dedupe_key=dedupe_key or f"view:{new_token()}",
                                  view=record if ok else None)  # fmt: skip
