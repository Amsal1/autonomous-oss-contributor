"""Rich (Bot API 10.2/10.3 ``blocks``) renderer of a ``View`` and the fallback decision (DESIGN §14.17.3).

* Every leaf is a plain ``RichText`` string: untrusted text is redacted and made visible first (U14,
  U1), ``skip_entity_detection`` is always set, links are explicit ``RichTextUrl`` (http(s) only).
  No ``html``/``markdown`` field exists in ``blocks.InputRichMessage``.
* ``use_rich``: ``ux_prefs.rich_views`` (auto|on|off, default from ``telegram.yaml``) and
  ``rich_unavailable_until``; approval cards and confirms never set ``View.rich`` (U3).
* ``mark_unavailable`` is called on a rich 400: classic for 24 h (§14.20.5).
"""

from __future__ import annotations

from datetime import timedelta

from ossbot.common.clock import to_iso
from ossbot.gatekeeper.chat import md2blocks
from ossbot.gatekeeper.telegram.escape import make_visible
from ossbot.gatekeeper.telegram.prefs import UxPrefs
from ossbot.gatekeeper.telegram.ui import blocks as rb
from ossbot.gatekeeper.telegram.ui import redact as ui_redact
from ossbot.gatekeeper.telegram.ui.kit import (
    Block,
    Bold,
    Code,
    Const,
    DetailsEl,
    Element,
    FooterEl,
    Italic,
    Line,
    Lines,
    Link,
    Markdown,
    PreBlock,
    Quote,
    Span,
    TableEl,
    Text,
    Time,
    Verbatim,
    View,
    ViewTooLarge,
)
from ossbot.gatekeeper.telegram.ui.limits import RICH_PRE_MAX, cut
from ossbot.gatekeeper.telegram.ui.sanitize import domain_of

RICH_FALLBACK_WINDOW = timedelta(hours=24)


def _visible(text: str) -> str:
    return make_visible(text)[0]


def _span(span: Span, surface: str) -> rb.RichText:
    if isinstance(span, Text):
        text, _ = ui_redact.redact(span.value, surface=surface)
        return _visible(cut(text, span.max) if span.max is not None else text)
    if isinstance(span, Const):
        return span.value
    if isinstance(span, Verbatim):
        ui_redact.scan(span.value, surface=span.surface)
        return _visible(span.value)
    if isinstance(span, Bold):
        return rb.RichTextBold(text=_span(span.inner, surface))
    if isinstance(span, Italic):
        return rb.RichTextItalic(text=_span(span.inner, surface))
    if isinstance(span, Code):
        return rb.RichTextCode(text=_span(span.inner, surface))
    if isinstance(span, Link):
        inner = _span(span.inner, surface)
        return rb.RichTextUrl(text=inner, url=span.url) if domain_of(span.url) else inner
    if isinstance(span, Time):
        return rb.RichTextDateTime(text=span.fallback, unix_time=span.unix, date_time_format=span.fmt)
    raise TypeError(f"{type(span).__name__} can't be rendered rich")  # Html spans are classic only


def _line(ln: Line, surface: str) -> rb.RichText:
    parts = [_span(s, surface) for s in ln]
    return parts[0] if len(parts) == 1 else parts


def _element(el: Element, surface: str) -> list[rb.Block]:
    if isinstance(el, Lines):
        return [rb.Paragraph(text=_line(ln, surface)) for ln in el.lines]
    if isinstance(el, PreBlock):
        if el.verbatim:
            ui_redact.scan(el.text, surface="pre")
            body = _visible(el.text)
        else:
            body = _visible(ui_redact.redact(el.text, surface=surface)[0])
        chunks = [body[i : i + RICH_PRE_MAX] for i in range(0, len(body), RICH_PRE_MAX)] or [""]
        return [rb.Pre(text=c, language=el.language) for c in chunks]
    if isinstance(el, Quote):
        paragraphs: list[rb.Block] = [rb.Paragraph(text=_line(ln, surface)) for ln in el.lines]
        if el.expandable:
            texts: list[rb.RichText] = []
            for i, ln in enumerate(el.lines):
                if i:
                    texts.append("\n")
                texts.append(_line(ln, surface))
            return [rb.ExpandableBlockQuote(text=texts)]
        return [rb.BlockQuote(blocks=paragraphs)]
    if isinstance(el, FooterEl):
        return [rb.Footer(text=_line(el.line, surface))]
    if isinstance(el, Markdown):
        text, _ = ui_redact.redact(el.text, surface=surface)
        if el.quoted:
            return [rb.BlockQuote(blocks=[rb.Paragraph(text=_visible(p)) for p in text.split("\n") if p.strip()]
                                  or [rb.Paragraph(text=" ")])]  # fmt: skip
        return md2blocks.rich_blocks(text) or [rb.Paragraph(text=" ")]
    if isinstance(el, DetailsEl):
        return [rb.Details(summary=_line(el.summary, surface),
                           blocks=[rb.Paragraph(text=_line(ln, surface)) for ln in el.lines])]  # fmt: skip
    if isinstance(el, TableEl):
        cells: list[list[rb.TableCell]] = [[rb.TableCell(text=_visible(c), is_header=True) for c in el.header]]
        cells += [[rb.TableCell(text=_visible(c) or None, align="left" if i == 0 else "right")
                   for i, c in enumerate(r)] for r in el.rows]  # fmt: skip
        return [rb.Table(cells=cells, is_bordered=True, is_compact=True)]
    raise TypeError(type(el).__name__)


def render(view: View, *, surface: str | None = None) -> rb.InputRichMessage:
    """Optional blocks are dropped (lowest priority first) until the message fits the rich budget."""
    keep: list[Block] = list(view.blocks)
    while True:
        out: list[rb.Block] = [blk for b in keep for el in b.elements for blk in _element(el, surface or view.kind)]
        message = rb.InputRichMessage(blocks=out or [rb.Paragraph(text=" ")])
        try:
            rb.check_budget(message)
            return message
        except rb.RichBudgetError:
            optional = [b for b in keep if not b.required]
            if not optional:
                raise ViewTooLarge(view.kind, {"blocks": len(out)}) from None
            keep.remove(min(optional, key=lambda b: b.priority))


def use_rich(view: View, prefs: UxPrefs, default: str) -> bool:
    if not view.rich:
        return False
    if prefs.until("rich_unavailable_until") is not None:
        return False
    if view.kind in {"file", "interdiff"} and prefs.get("file_view", "auto") == "classic":
        return False  # §14.23 item 1: line comments by quote-reply on classic file views
    mode = prefs.get("rich_views", default)
    return mode in {"auto", "on"}


def mark_unavailable(prefs: UxPrefs) -> None:
    prefs.set("rich_unavailable_until", to_iso(prefs.clock.now() + RICH_FALLBACK_WINDOW))
