"""The copyable-id lint (DESIGN §14.17.3): every prefixed id (``c_…``, ``a_…``, ``x_…`` …) the bot shows must
be one tap to copy, i.e. inside ``<code>``/``<pre>`` in classic HTML or a ``code``/``pre`` node in a rich
payload. Ids rendered outside ``ui/ids.py`` (``k.Id``, ``ids.html``, ``ids.autocode``) fail it.

Exempt by design: button labels and ``reply_markup`` (Telegram can't format them), callback toasts
(``answerCallbackQuery`` is plain text), and attribute values such as an ``href`` (not shown as text).
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

ID = re.compile(r"\b[a-z]_[0-9A-HJKMNP-TV-Z]{10}\b")
TEXT_METHODS = {"sendMessage", "editMessageText", "sendMessageDraft"}
CAPTION_METHODS = {"sendPhoto", "sendDocument", "sendVideo", "sendAnimation", "editMessageCaption"}
RICH_METHODS = {"sendRichMessage", "sendRichMessageDraft"}  # and editMessageText carrying ``rich_message``
_TAG = re.compile(r"<(/?)([a-zA-Z-]+)[^>]*>")
_COPYABLE_TAGS = {"code", "pre"}


def bare_ids_html(text: str) -> list[str]:
    """Ids in classic HTML text that sit outside every ``<code>``/``<pre>`` (tags and attributes skipped)."""
    out: list[str] = []
    depth = 0
    pos = 0
    for m in _TAG.finditer(text):
        if depth == 0:
            out += ID.findall(html.unescape(text[pos : m.start()]))
        if m.group(2).lower() in _COPYABLE_TAGS:
            depth += -1 if m.group(1) else 1
            depth = max(depth, 0)
        pos = m.end()
    if depth == 0:
        out += ID.findall(html.unescape(text[pos:]))
    return out


def bare_ids_plain(text: str) -> list[str]:
    """Plain text (no parse mode) can't hold code: every id in it is bare."""
    return ID.findall(text)


def bare_ids_rich(node: Any, *, inside: bool = False) -> list[str]:
    """Ids in a rich payload's text leaves outside a ``code``/``pre`` node."""
    if isinstance(node, str):
        return [] if inside else ID.findall(node)
    if isinstance(node, list):
        return [i for x in node for i in bare_ids_rich(x, inside=inside)]
    if isinstance(node, dict):
        here = inside or node.get("type") in _COPYABLE_TAGS
        out: list[str] = []
        for key, value in node.items():
            if key in {"type", "url", "language", "date_time_format", "align", "valign"}:
                continue
            out += bare_ids_rich(value, inside=here)
        return out
    return []


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def bare_ids(method: str, payload: dict[str, Any]) -> list[str]:
    """Every id a Bot API call would show that is not one tap to copy (empty = the call passes)."""
    if method in RICH_METHODS or "rich_message" in payload:
        return bare_ids_rich(_maybe_json(payload.get("rich_message")))
    if method in TEXT_METHODS or method in CAPTION_METHODS:
        key = "text" if method in TEXT_METHODS else "caption"
        text = str(payload.get(key) or "")
        if str(payload.get("parse_mode") or "").upper() == "HTML":
            return bare_ids_html(text)
        if payload.get("entities") or payload.get("caption_entities"):
            return []  # entity-formatted text: checked where it is built
        return bare_ids_plain(text)
    return []  # answerCallbackQuery, reply markup edits, pins, topics: nothing formatted to check


def assert_ids_copyable(method: str, payload: dict[str, Any]) -> None:
    bare = bare_ids(method, payload)
    assert not bare, f"{method}: id(s) {bare} rendered outside <code> (use ui/ids.py): {payload!r:.600}"
