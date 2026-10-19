"""Classic (parse mode HTML) renderer of a ``View`` (DESIGN §14.17.3, U1, U8, U14).

Every untrusted leaf (``Text``) is redacted first, then made visible, defused and escaped; ``Const``
template text is only escaped; ``Verbatim`` publish bytes are scanned (a hit is a bug, counted in
``Rendered.verbatim_hits``) and made visible, never redacted or defused. Tags come only from this
module: ``b i code pre a blockquote(expandable) tg-time``; links only for http(s).
"""

from __future__ import annotations

from dataclasses import dataclass

from ossbot.gatekeeper.telegram.escape import defuse_commands, esc, esc_attr, make_visible, tg_time
from ossbot.gatekeeper.telegram.ui import redact as ui_redact
from ossbot.gatekeeper.telegram.ui.kit import (
    Block,
    Bold,
    Code,
    Const,
    Element,
    FooterEl,
    Html,
    Italic,
    Line,
    Lines,
    Link,
    PreBlock,
    Quote,
    Span,
    Text,
    Time,
    Verbatim,
    View,
    ViewTooLarge,
)
from ossbot.gatekeeper.telegram.ui.limits import CLASSIC_MAX, cut
from ossbot.gatekeeper.telegram.ui.sanitize import domain_of


@dataclass
class Rendered:
    text: str
    redacted: int = 0
    verbatim_hits: int = 0
    dropped: tuple[str, ...] = ()


class _Ctx:
    def __init__(self, surface: str) -> None:
        self.surface = surface
        self.redacted = 0
        self.verbatim_hits = 0

    def untrusted(self, value: str, limit: int | None) -> str:
        text, hits = ui_redact.redact(value, surface=self.surface)
        self.redacted += hits
        if limit is not None:
            text = cut(text, limit)
        visible, _ = make_visible(text)
        return esc(defuse_commands(visible))

    def verbatim(self, value: str, surface: str) -> str:
        self.verbatim_hits += ui_redact.scan(value, surface=surface)
        return esc(make_visible(value)[0])


def _span(span: Span, c: _Ctx) -> str:
    if isinstance(span, Text):
        return c.untrusted(span.value, span.max)
    if isinstance(span, Const):
        return esc(span.value)
    if isinstance(span, Verbatim):
        return c.verbatim(span.value, span.surface)
    if isinstance(span, Bold):
        return f"<b>{_span(span.inner, c)}</b>"
    if isinstance(span, Italic):
        return f"<i>{_span(span.inner, c)}</i>"
    if isinstance(span, Code):
        return f"<code>{_span(span.inner, c)}</code>"
    if isinstance(span, Link):
        inner = _span(span.inner, c)
        return f'<a href="{esc_attr(span.url)}">{inner}</a>' if domain_of(span.url) else inner
    if isinstance(span, Time):
        return tg_time(span.unix, span.fallback, span.fmt)
    if isinstance(span, Html):
        return span.value
    raise TypeError(type(span).__name__)


def _line(ln: Line, c: _Ctx) -> str:
    return "".join(_span(s, c) for s in ln)


def _element(el: Element, c: _Ctx) -> str:
    if isinstance(el, Lines):
        return "\n".join(_line(ln, c) for ln in el.lines)
    if isinstance(el, PreBlock):
        body = c.verbatim(el.text, "pre") if el.verbatim else c.untrusted(el.text, None)
        if el.language:
            return f'<pre><code class="language-{esc_attr(el.language)}">{body}</code></pre>'
        return f"<pre>{body}</pre>"
    if isinstance(el, Quote):
        inner = "\n".join(_line(ln, c) for ln in el.lines)
        return f"<blockquote expandable>{inner}</blockquote>" if el.expandable else f"<blockquote>{inner}</blockquote>"
    if isinstance(el, FooterEl):
        return f"<i>{_line(el.line, c)}</i>"
    raise TypeError(type(el).__name__)


def _block(block: Block, c: _Ctx) -> str:
    return "\n".join(_element(el, c) for el in block.elements)


def render(view: View, *, budget: int = CLASSIC_MAX, surface: str | None = None) -> Rendered:
    """The budget rule (§14.18.1): optional blocks are dropped lowest priority first until the text fits;
    required blocks are never cut. Still too large -> ``ViewTooLarge`` (a bug)."""
    keep = list(view.blocks)
    dropped: list[str] = []
    while True:
        c = _Ctx(surface or view.kind)
        parts = [_block(b, c) for b in keep]
        text = "\n".join(p for p in parts if p)
        if len(text) <= budget:
            return Rendered(text, c.redacted, c.verbatim_hits, tuple(dropped))
        optional = [b for b in keep if not b.required]
        if not optional:
            sizes = {b.name or f"block{i}": len(p) for i, (b, p) in enumerate(zip(keep, parts, strict=True))}
            raise ViewTooLarge(view.kind, sizes)
        victim = min(optional, key=lambda b: b.priority)
        keep.remove(victim)
        dropped.append(victim.name or "optional")
