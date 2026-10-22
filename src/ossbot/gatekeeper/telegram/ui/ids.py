"""Copyable identifiers (DESIGN §14.17.3, §14.18.1): cids, approval and action ids, SHAs, PR refs and
branch names render as classic ``<code>`` or a rich ``RichTextCode`` node, so one tap copies them.

* ``k.Id(value, kind)`` is the kit span; ``html(value, kind)`` is the same for gatekeeper-built classic
  HTML (the S12 stale header). Gatekeeper-generated kinds are checked against their grammar here; a
  mismatch raises ``LimitError`` (a bug in code, never shown).
* ``branch`` is the one untrusted kind: redacted (``ui/redact.py``), made visible and escaped like
  ``k.Text`` before it is wrapped.
* ``autocode(text)`` escapes plain text and wraps only strong id patterns (prefixed ids, 40-hex SHAs);
  ``spans(text)`` splits text into ``Const``/``Id`` spans the same way. Neither is ever applied to
  ``Verbatim`` publish bytes (U8, I12). Toasts and button labels stay plain (Telegram can't format them).
"""

from __future__ import annotations

import re

from ossbot.gatekeeper.telegram.escape import esc, make_visible
from ossbot.gatekeeper.telegram.ui import redact as ui_redact
from ossbot.gatekeeper.telegram.ui.kit import Const, Id, IdKind, Line, Span
from ossbot.gatekeeper.telegram.ui.limits import LimitError

_PREFIXED = "[0-9A-HJKMNP-TV-Z]{10}"
PATTERNS: dict[str, re.Pattern[str]] = {
    "id": re.compile(rf"[a-z]_{_PREFIXED}"),
    "cid": re.compile(rf"c_{_PREFIXED}"),
    "approval": re.compile(rf"a_{_PREFIXED}"),
    "proposal": re.compile(rf"p_{_PREFIXED}"),
    "skill_approval": re.compile(rf"s_{_PREFIXED}"),
    "action": re.compile(rf"x_{_PREFIXED}"),
    "hold": re.compile(rf"h_{_PREFIXED}"),
    "run": re.compile(rf"r_{_PREFIXED}"),
    "skill": re.compile(r"[a-z0-9-]{1,64}"),
    "sha": re.compile(r"[0-9a-f]{7,40}"),
    "pr": re.compile(r"#[0-9]{1,9}"),
    "branch": re.compile(r"[A-Za-z0-9._/-]{1,100}"),
}
KIND_OF_PREFIX: dict[str, IdKind] = {"c": "cid", "a": "approval", "p": "proposal", "s": "skill_approval",
                                     "x": "action", "h": "hold", "r": "run"}  # fmt: skip
# Strong patterns only: a prefixed id or a full 40-hex SHA standing alone (``xc_…`` lookalikes don't match).
STRONG = re.compile(rf"\b(?:[a-z]_{_PREFIXED}|[0-9a-f]{{40}})\b")


def valid(value: str, kind: IdKind) -> bool:
    pattern = PATTERNS.get(kind)
    if pattern is None or not pattern.fullmatch(value):
        return False
    return not (kind == "branch" and ".." in value)


def check(value: str, kind: IdKind) -> str:
    """The value when it matches its kind's grammar, else ``LimitError`` (a bug: ids are ours)."""
    if not valid(value, kind):
        raise LimitError(f"{kind} id {value[:40]!r} does not match its grammar")
    return value


def inner_html(value: str, kind: IdKind) -> str:
    """The escaped text inside ``<code>``: ``branch`` is redacted and made visible first (untrusted)."""
    if kind == "branch":
        text, _ = ui_redact.redact(value, surface="id")
        return esc(make_visible(text)[0])
    return esc(check(value, kind))


def rich_text(value: str, kind: IdKind) -> str:
    """The plain text of the rich code node (the renderer wraps it in ``RichTextCode``)."""
    if kind == "branch":
        text, _ = ui_redact.redact(value, surface="id")
        return make_visible(text)[0]
    return check(value, kind)


def html(value: str, kind: IdKind = "cid") -> str:
    """``<code>value</code>`` for gatekeeper-built classic HTML."""
    return f"<code>{inner_html(value, kind)}</code>"


def _kind(match: str) -> IdKind:
    if len(match) == 40 and "_" not in match:
        return "sha"
    return KIND_OF_PREFIX.get(match[0], "id")


def autocode(text: str) -> str:
    """Plain (untrusted) text as classic HTML: escaped, with strong id patterns wrapped in ``<code>``."""
    out: list[str] = []
    pos = 0
    for m in STRONG.finditer(text):
        out.append(esc(text[pos : m.start()]))
        out.append(f"<code>{esc(m.group(0))}</code>")
        pos = m.end()
    out.append(esc(text[pos:]))
    return "".join(out)


def spans(text: str) -> Line:
    """Template text split into ``Const`` and ``Id`` spans around strong id patterns."""
    out: list[Span] = []
    pos = 0
    for m in STRONG.finditer(text):
        if m.start() > pos:
            out.append(Const(text[pos : m.start()]))
        out.append(Id(m.group(0), _kind(m.group(0))))
        pos = m.end()
    if pos < len(text) or not out:
        out.append(Const(text[pos:]))
    return tuple(out)
