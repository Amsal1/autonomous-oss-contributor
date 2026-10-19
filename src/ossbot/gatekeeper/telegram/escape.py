"""HTML escaping for Telegram (parse mode HTML, DESIGN §14.3, §14.17.3).

* Text nodes: ``html.escape(text, quote=False)``; attribute values: ``quote=True``.
* Bytes that will be published (PR body, commit message, comments) are shown verbatim inside
  ``<pre>``; invisible, bidi and control characters are first made visible as ``⟦U+XXXX⟧`` and
  counted, so nothing hidden can ride along unseen.
"""

from __future__ import annotations

import html
import re

# Zero-width, bidi embedding/override/isolate, word joiner/invisible operators, BOM, soft hyphen,
# Mongolian vowel separator, and C0/C1 controls other than \n and \t.
_INVISIBLE = re.compile(
    "[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff"
    "\x00-\x08\x0b-\x1f\x7f-\x9f\ufff9-\ufffb]"
)


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def esc_attr(text: str) -> str:
    return html.escape(text, quote=True)


def make_visible(text: str) -> tuple[str, int]:
    """Replace every invisible/bidi/control character by ``⟦U+XXXX⟧``; return the count too."""
    count = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"⟦U+{ord(match.group(0)):04X}⟧"

    return _INVISIBLE.sub(repl, text), count


def pre(text: str) -> str:
    """Verbatim block: visible pre-pass, then escaped inside ``<pre>``."""
    visible, _ = make_visible(text)
    return f"<pre>{esc(visible)}</pre>"


def code(text: str) -> str:
    visible, _ = make_visible(text)
    return f"<code>{esc(visible)}</code>"


def b(text: str) -> str:
    return f"<b>{esc(text)}</b>"


WORD_JOINER = "\u2060"
_COMMAND_LINK = re.compile(r"/(?=[A-Za-z])")


def defuse_commands(text: str) -> str:
    """U8 (DESIGN §14.9): a WORD JOINER after every ``/`` that a letter follows, so ``/kill`` inside
    untrusted classic text is not a tappable bot command. Display only: never applied to bytes that
    will be published (``<pre>`` parts, the PR title). Run after ``make_visible`` (which would
    otherwise show the marker as ``⟦U+2060⟧``); ``ui.sanitize.undefuse`` removes it on the way back."""
    return _COMMAND_LINK.sub("/" + WORD_JOINER, text)


def untrusted(text: str) -> str:
    """One untrusted classic text leaf, already redacted by the caller: visible, defused, escaped."""
    visible, _ = make_visible(text)
    return esc(defuse_commands(visible))


TG_TIME_FORMAT_RE = re.compile(r"^(r|w?[dD]?[tT]?)\Z")  # Bot API "date-time entity formatting" (verified 2026-10-19)


def tg_time(unix: int, fallback: str, fmt: str = "r") -> str:
    """``<tg-time>`` (Bot API date-time entity): clients render it relative ("3 hours ago") or in the
    reader's zone; ``fallback`` is shown by clients without support. ``fmt`` "r" = relative."""
    if not TG_TIME_FORMAT_RE.match(fmt):
        raise ValueError(f"bad tg-time format {fmt!r}")
    return f'<tg-time unix="{int(unix)}" format="{fmt}">{esc(fallback)}</tg-time>'
