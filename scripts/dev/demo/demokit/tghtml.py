"""Telegram classic HTML (``parse_mode: HTML``) -> display HTML, plus the text Telegram shows.

Only the tags the gatekeeper emits are accepted (``telegram/ui/classic.py``, ``telegram/escape.py``,
``chat/md2blocks.py classic_html``): b i u s code pre a blockquote[expandable] tg-time. Every text node
is re-escaped; links become styled spans (never live links); ``<tg-time>`` shows its fallback text, as
clients without date-time support do. Any other tag fails (fail closed).
"""

from __future__ import annotations

import html
from html.parser import HTMLParser

INLINE = {"b": "b", "strong": "b", "i": "i", "em": "i", "u": "u", "ins": "u", "s": "s", "strike": "s", "del": "s"}
ALLOWED = frozenset({*INLINE, "code", "pre", "a", "blockquote", "tg-time"})


class TelegramHtmlError(ValueError):
    pass


class _Walker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.text: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED:
            raise TelegramHtmlError(f"unexpected tag <{tag}>")
        names = {k for k, _ in attrs}
        self.stack.append(tag)
        if tag in INLINE:
            self.out.append(f"<{INLINE[tag]}>")
        elif tag == "code":
            self.out.append('<code class="tg-code">')
        elif tag == "pre":
            self.out.append('<span class="tg-pre">')
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            cls = "tg-link" if href.startswith(("https://", "http://")) else "tg-plain"
            self.out.append(f'<span class="{cls}">')
        elif tag == "blockquote":
            self.out.append('<span class="tg-quote tg-exp">' if "expandable" in names else '<span class="tg-quote">')
        else:  # tg-time: the fallback text
            self.out.append('<span class="tg-time">')

    def handle_endtag(self, tag: str) -> None:
        if tag not in ALLOWED:
            raise TelegramHtmlError(f"unexpected tag </{tag}>")
        if not self.stack or self.stack[-1] != tag:
            raise TelegramHtmlError(f"unbalanced </{tag}>")
        self.stack.pop()
        self.out.append(f"</{INLINE[tag]}>" if tag in INLINE else ("</code>" if tag == "code" else "</span>"))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raise TelegramHtmlError(f"unexpected empty tag <{tag}/>")

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        self.out.append(html.escape(data, quote=False))

    def handle_comment(self, data: str) -> None:
        raise TelegramHtmlError("comment in Telegram HTML")

    def handle_decl(self, decl: str) -> None:
        raise TelegramHtmlError("declaration in Telegram HTML")

    def handle_pi(self, data: str) -> None:
        raise TelegramHtmlError("processing instruction in Telegram HTML")

    def unknown_decl(self, data: str) -> None:
        raise TelegramHtmlError("unknown declaration in Telegram HTML")


def _walk(telegram_html: str) -> _Walker:
    walker = _Walker()
    walker.feed(telegram_html)
    walker.close()
    if walker.stack:
        raise TelegramHtmlError(f"unclosed <{walker.stack[-1]}>")
    return walker


def to_html(telegram_html: str) -> str:
    """Display HTML: only the allowlisted tags (as spans and inline elements), every text escaped."""
    return "".join(_walk(telegram_html).out)


def plain_text(telegram_html: str) -> str:
    """The text a Telegram client shows: entities decoded, tags removed."""
    return "".join(_walk(telegram_html).text)
