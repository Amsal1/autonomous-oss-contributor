"""Inline keyboards. Every button carries an opaque ``t:<22-char token>`` (24 bytes; DESIGN §14.17.4, §14.18.2).

Card rows per action kind (§14.11.1, §14.18.4.1): row 1 the decision (Approve styled ``success``),
row 2 explore and files (or interdiff), row 3 edit. ``comment``/``issue_claim`` cards have no Explore,
Files or Request changes; ``push_update`` has no Edit text.
"""

from __future__ import annotations

import re
from typing import Any

from ossbot.common.schemas import OutboundKind

CALLBACK_RE = re.compile(r"^t:[A-Za-z0-9_-]{22}\Z")
# rows of (label, token) or (label, token, style); style is a Bot API 10.3 button colour
Button = tuple[str, str] | tuple[str, str, str]
Rows = list[list[Button]]
DISABLED = ""  # the token of a disabled button (no callback)
MIRROR_URL_PREFIX = "https://github.com/"  # a URL button (row 4, §14.11.14); tokens never contain ':' or '/'

APPROVE_LABEL: dict[OutboundKind, str] = {
    "open_pr": "✅ Approve & Submit",
    "push_update": "✅ Approve & Push",
    "comment": "✅ Approve & Post",
    "issue_claim": "✅ Approve & Post",
}
REJECT_LABEL: dict[OutboundKind, str] = {
    "open_pr": "❌ Reject",
    "push_update": "❌ Reject fix",
    "comment": "❌ Reject",
    "issue_claim": "❌ Reject",
}


def callback_data(token: str) -> str:
    data = f"t:{token}"
    if not CALLBACK_RE.match(data):
        raise ValueError("bad token")
    return data


def token_of(data: str | None) -> str | None:
    if data is None or not CALLBACK_RE.match(data):
        return None
    return data[2:]


def card_rows(kind: OutboundKind, tokens: dict[str, str], *, files: int | None = None,
              mirror_url: str | None = None) -> Rows:  # fmt: skip
    """The S1 card keyboard for ``card_purposes(kind, has_interdiff)`` (§14.18.4.1)."""
    rows: Rows = [[(APPROVE_LABEL[kind], tokens["approve"], "success"), (REJECT_LABEL[kind], tokens["reject"])]]
    if kind in {"open_pr", "push_update"}:
        second = ("🔀 Interdiff", tokens["interdiff"]) if "interdiff" in tokens else (
            f"🧾 Files ({files})" if files is not None else "🧾 Files", tokens["files"])  # fmt: skip
        rows.append([("🧭 Explore", tokens["explore"]), second])
        third: list[Button] = [("✏️ Request changes", tokens["changes"])]
        if kind == "open_pr":
            third.append(("📝 Edit text", tokens["edit_text_pick"]))
        rows.append(third)
        if mirror_url:  # §14.11.14: row 4, a URL button (no token), only while the mirror is on and pushed
            rows.append([("🐙 Review on GitHub ↗", mirror_url)])
    else:
        rows.append([("📝 Edit text", tokens["edit_text_pick"])])
    return rows


def confirm_rows(confirm: str, cancel: str, *, live: bool | None = None) -> Rows:
    """S8: ``[🧪 Confirm dry run](primary)`` or ``[🚀 Confirm LIVE](danger)``, then ``[↩️ Cancel]``."""
    if live is None:
        return [[("✅ Confirm", confirm), ("↩️ Cancel", cancel)]]
    first: Button = ("🚀 Confirm LIVE", confirm, "danger") if live else ("🧪 Confirm dry run", confirm, "primary")
    return [[first, ("↩️ Cancel", cancel)]]


def single_row(label: str, token: str) -> Rows:
    return [[(label, token)]]


def button_json(button: Button) -> dict[str, Any]:
    if button[1] == DISABLED:  # a Bot API 10.3 DisabledButton (e.g. ⛔ Not startable)
        return {"text": button[0], "disabled": {}}
    if button[1].startswith(MIRROR_URL_PREFIX):  # the review mirror's URL button (never a token)
        return {"text": button[0], "url": button[1]}
    out: dict[str, Any] = {"text": button[0], "callback_data": callback_data(button[1])}
    if len(button) == 3:
        out["style"] = button[2]
    return out


def to_json(rows: Rows) -> dict[str, Any]:
    """``reply_markup`` for raw Bot API payloads (outbox)."""
    return {"inline_keyboard": [[button_json(b) for b in row] for row in rows]}


def disabled_markup(label: str) -> dict[str, Any]:
    """One Bot API 10.3 ``DisabledButton`` (delivering, not approvable)."""
    return {"inline_keyboard": [[{"text": label, "disabled": {}}]]}
