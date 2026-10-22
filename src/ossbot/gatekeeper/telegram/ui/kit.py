"""The UI component kit (DESIGN §14.18.1): every gatekeeper view is a ``View`` built from these parts.

* Spans carry their trust: ``Text`` (untrusted or model text: redacted, made visible, defused,
  escaped), ``Const`` (gatekeeper template text: escaped only, so our own ``/resume`` hints stay
  tappable), ``Verbatim`` (publish bytes such as the PR title: scanned, made visible, escaped, never
  redacted, defused or cut), ``Id`` (a gatekeeper id, SHA, PR ref or branch shown one tap to copy). The
  renderers also put the ids found in ``Const`` text in code (``ui/ids.py``), never those in ``Text`` or
  ``Verbatim``.
* A view is a list of ``Block``s marked required or optional. The budget rule drops optional blocks
  lowest priority first and never cuts inside a block; required blocks are bounded by their spans'
  ``max``. ``ViewTooLarge`` means a bug: the caller logs ERROR and sends ``stub`` instead.
* Buttons are ``ButtonSpec``s; only the gatekeeper mints their tokens (``ui.send``, U2). Labels follow
  the one row rule of §14.17.2 (``limits.check_row``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any, Literal

from ossbot.gatekeeper.telegram.ui import icons
from ossbot.gatekeeper.telegram.ui.limits import CLASSIC_MAX, check_keyboard

Style = Literal["danger", "success", "primary"]
DEFAULT_TTL = timedelta(hours=24)


# ------------------------------------------------------------------------------------ spans
@dataclass(frozen=True)
class Text:
    """Untrusted or model-authored text; ``max`` cuts it with "…" for display (never publish bytes)."""

    value: str
    max: int | None = None


@dataclass(frozen=True)
class Const:
    """Gatekeeper template text (no interpolated untrusted content)."""

    value: str


@dataclass(frozen=True)
class Verbatim:
    """Publish bytes (PR title, a quoted body): scanned, never altered (U8, U14)."""

    value: str
    surface: str = "verbatim"


@dataclass(frozen=True)
class Bold:
    inner: Span


@dataclass(frozen=True)
class Italic:
    inner: Span


@dataclass(frozen=True)
class Code:
    inner: Span


@dataclass(frozen=True)
class Link:
    inner: Span
    url: str  # http(s) only; anything else renders as plain text


@dataclass(frozen=True)
class Time:
    unix: int
    fallback: str
    fmt: str = "r"


@dataclass(frozen=True)
class Html:
    """Gatekeeper-built classic HTML (a command ``Reply``, already escaped with ``escape.esc``). Views
    holding it are classic only (the rich renderer refuses it)."""

    value: str


# ``ui/ids.py`` holds the grammar of each kind; ``branch`` is the one untrusted kind (redacted first).
IdKind = Literal["id", "cid", "approval", "proposal", "skill_approval", "action", "hold", "run", "skill", "sha",
                 "pr", "branch"]  # fmt: skip


@dataclass(frozen=True)
class Id:
    """An identifier shown one tap to copy: classic ``<code>``, rich ``RichTextCode`` (``ui/ids.py``).
    Gatekeeper-generated kinds are checked against their grammar (a mismatch raises ``LimitError``: a
    bug); ``branch`` is untrusted and redacted like ``Text``. Never used for publish bytes (U8)."""

    value: str
    kind: IdKind = "cid"


Span = Text | Const | Verbatim | Bold | Italic | Code | Link | Time | Html | Id
Line = tuple[Span, ...]


def line(*parts: Span | str) -> Line:
    """A line; bare ``str`` parts are ``Const`` (template text)."""
    return tuple(Const(p) if isinstance(p, str) else p for p in parts)


# --------------------------------------------------------------------------------- elements
@dataclass(frozen=True)
class Lines:
    lines: tuple[Line, ...]


@dataclass(frozen=True)
class PreBlock:
    text: str
    verbatim: bool = False  # publish bytes: scanned, not redacted
    language: str | None = None


@dataclass(frozen=True)
class Quote:
    lines: tuple[Line, ...]
    expandable: bool = False


@dataclass(frozen=True)
class FooterEl:
    line: Line


@dataclass(frozen=True)
class Markdown:
    """Model-authored Markdown (a Hermes answer): the renderers redact the whole text first (U14), then
    convert it with ``chat/md2blocks`` (§14.12.2). ``quoted`` puts the whole body in one quote as plain
    text (flat mode and ✅ Reviews, U6)."""

    text: str
    quoted: bool = False


@dataclass(frozen=True)
class DetailsEl:
    """A closed ``details`` block (rich) or a bold summary plus an expandable quote (classic)."""

    summary: Line
    lines: tuple[Line, ...]


@dataclass(frozen=True)
class TableEl:
    """Gatekeeper facts in columns: a rich ``table`` (header row first) or a classic aligned ``<pre>``."""

    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


Element = Lines | PreBlock | Quote | FooterEl | Markdown | DetailsEl | TableEl


@dataclass(frozen=True)
class Block:
    elements: tuple[Element, ...]
    required: bool = True
    priority: int = 0  # optional blocks are dropped lowest priority first
    name: str = ""


# ---------------------------------------------------------------------------------- buttons
@dataclass(frozen=True)
class ButtonSpec:
    label: str
    purpose: str | None = None  # a callback token purpose (minted by ui.send)
    arg: Mapping[str, Any] | None = None
    style: Style | None = None
    single_use: bool = False
    ttl: timedelta = DEFAULT_TTL
    url: str | None = None
    copy_text: str | None = None
    disabled: bool = False
    token: str | None = None  # an already minted and bound token (card rows, U3)
    raw_arg: str | None = None  # stored verbatim as ui_tokens.arg (the registry's Confirm(arg) strings)


Rows = list[list[ButtonSpec]]


@dataclass
class View:
    kind: str  # a ui.send.ViewKind; validated at enqueue
    blocks: list[Block]
    rows: Rows = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    cid: str | None = None
    rev: int | None = None
    action_id: str | None = None
    approval_id: str | None = None
    path: str | None = None
    page: int | None = None
    rich: bool = False  # may be rendered rich (never approval cards or confirms, U3)
    placeholder: str | None = None  # ForceReply prompt (classic only)
    max_rows: int = 6
    link_preview: str | None = None  # classic only: the one source an answer cites (§14.12.3)

    def keyboard_labels(self) -> list[list[str]]:
        return [[b.label for b in row] for row in self.rows]

    def check(self) -> None:
        check_keyboard(self.keyboard_labels(), max_rows=self.max_rows)


class ViewTooLarge(Exception):  # DESIGN §14.18.1 names it
    def __init__(self, kind: str, sizes: Mapping[str, int]) -> None:
        super().__init__(f"{kind}: {dict(sizes)}")
        self.kind = kind
        self.sizes = dict(sizes)


# ------------------------------------------------------------------------------- components
def mode_badge(dry_run: bool | None, *, na: str | None = None) -> Span:
    """🧪 DRY RUN / 🔴 LIVE, with "(n/a: <why>)" for actions dry-run does not govern (S3)."""
    if dry_run is None:
        return Const("")
    text = f"{icons.DRY_RUN} DRY RUN" if dry_run else f"{icons.LIVE} LIVE"
    return Bold(Const(text + (f" (n/a: {na})" if na else "")))


def header(icon: str, title: str, ident: str | None = None, mode: Span | None = None) -> Line:
    """``icon <b>title</b> · ident · mode``; the ident's ids (``c_…``, 40-hex SHAs, ``PR #12``) become ``Id``
    spans, one tap to copy (``ui/ids.py``)."""
    from ossbot.gatekeeper.telegram.ui import ids  # noqa: PLC0415 - ids builds on this module's spans

    parts: list[Span] = [Const(f"{icon} "), Bold(Const(title))]
    if ident:
        parts += [Const(" · "), *ids.spans(ident)]
    if mode is not None and mode != Const(""):
        parts += [Const(" · "), mode]
    return tuple(parts)


def fact(label: str, value: Span | str) -> Line:
    """A gatekeeper-verified fact (✅ facts carry no 🤖, U6)."""
    v = Const(value) if isinstance(value, str) else value
    return (Const(f"{label} "), v)


def claim(label: str, text: str, *, max_len: int = 300) -> Element:
    """Model-authored text: 🤖-prefixed and quoted (U6)."""
    return Quote(((Const(f"{icons.MODEL} "), Bold(Const(label)), Const(" "), Text(text, max_len)),))


def card(
    header_line: Line,
    subject: Sequence[Line],
    facts: Sequence[Line] = (),
    sections: Sequence[Block] = (),
    footer: Line | None = None,
) -> list[Block]:
    """``header · subject · facts · details · footer`` (§14.17.2); header and subject are required."""
    blocks = [Block((Lines((header_line, *subject)),), required=True, name="header")]
    if facts:
        blocks.append(Block((Lines(tuple(facts)),), required=True, name="facts"))
    blocks += list(sections)
    if footer is not None:
        blocks.append(Block((FooterEl(footer),), required=False, priority=1, name="footer"))
    return blocks


def toast(text: str) -> str:
    """Callback answer text (≤ 180)."""
    return text if len(text) <= 180 else text[:179] + "…"


def stub(text: str, kind: str = "fyi") -> View:
    """A superseded or withheld message: one fixed line, no buttons."""
    return View(kind=kind, blocks=[Block((Lines((line(text),)),))])


def plain(kind: str, *lines_: Line, rows: Rows | None = None) -> View:
    return View(kind=kind, blocks=[Block((Lines(tuple(lines_)),))], rows=rows or [])


def button(label: str, purpose: str, arg: Mapping[str, Any] | None = None, *, style: Style | None = None,
           single_use: bool = False, ttl: timedelta = DEFAULT_TTL) -> ButtonSpec:  # fmt: skip
    return ButtonSpec(label, purpose, dict(arg or {}), style, single_use, ttl)


def url_button(label: str, url: str) -> ButtonSpec:
    return ButtonSpec(label, url=url)


def copy_button(label: str, text: str) -> ButtonSpec:
    return ButtonSpec(label, copy_text=text)


def disabled_button(label: str) -> ButtonSpec:
    return ButtonSpec(label, disabled=True)


def bound(label: str, token: str, *, style: Style | None = None) -> ButtonSpec:
    """A button for an already minted, message-bound token (approval card rows, U3)."""
    return ButtonSpec(label, style=style, token=token)


def pager(purpose: str, arg: Mapping[str, Any], page: int, pages: int) -> list[ButtonSpec]:
    """``[◀] [i/N] [▶]``; the ends are disabled buttons (Bot API 10.3 ``DisabledButton``)."""
    prev = button("◀", purpose, {**arg, "page": page - 1}) if page > 1 else disabled_button("◀")
    nxt = button("▶", purpose, {**arg, "page": page + 1}) if page < pages else disabled_button("▶")
    return [prev, disabled_button(f"{page}/{pages}"), nxt]


def list_view(items: Sequence[Line], page: int, per_page: int) -> tuple[list[Line], int, int]:
    """(lines of ``page``, the clamped page, page count)."""
    pages = max(1, -(-len(items) // per_page))
    page = min(max(1, page), pages)
    start = (page - 1) * per_page
    return list(items[start : start + per_page]), page, pages


def confirm_view(sentence: str, *, kind: str = "confirm_prompt", mode: Span | None = None,
                 rows: Rows | None = None, subject: Line | None = None) -> View:  # fmt: skip
    head: list[Line] = []
    if mode is not None:
        head.append((mode,))
    if subject is not None:
        head.append(subject)
    head.append((Bold(Const(sentence)),))
    return View(kind=kind, blocks=[Block((Lines(tuple(head)),))], rows=rows or [])


def picker(options: Sequence[tuple[str, str, Mapping[str, Any]]], selected: set[str], *, multi: bool,
           per_row: int = 2) -> Rows:  # fmt: skip
    """Toggle grid: (value, label, token arg) -> buttons; selected ones are marked ✅ (multi) or •."""
    mark = "✅ " if multi else "• "
    buttons = [
        button((mark if value in selected else "") + label, str(arg.get("purpose", "picker")),
               {k: v for k, v in arg.items() if k != "purpose"})
        for value, label, arg in options
    ]  # fmt: skip
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def with_rows(view: View, rows: Rows) -> View:
    return replace(view, rows=rows)


__all__ = ["CLASSIC_MAX", "Block", "Bold", "ButtonSpec", "Code", "Const", "DetailsEl", "Element", "FooterEl", "Id",
           "IdKind", "Italic", "Line",
           "Lines", "Link", "Markdown", "PreBlock", "Quote", "Rows", "Span", "TableEl", "Text", "Time",
           "Verbatim", "View",
           "ViewTooLarge", "bound", "button", "card", "claim", "confirm_view", "copy_button", "disabled_button", "fact",
           "header", "line", "list_view", "mode_badge", "pager", "picker", "plain", "stub", "toast", "url_button",
           "with_rows"]  # fmt: skip
