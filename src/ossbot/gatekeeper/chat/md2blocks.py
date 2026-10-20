"""Hermes Markdown -> rich blocks or classic HTML (DESIGN §14.12.2, U1).

A strict, minimal, line-based parser over text that the caller has **already redacted** (U14). It
never emits images or media, raw HTML, ``tg://`` links or buttons: every text leaf is a plain string
(made visible here; classic leaves are also defused and escaped), and links are explicit
``RichTextUrl``/``<a>`` only for ``http(s)://`` URLs of at most 2,048 characters, shown as the link
text followed by the domain when the two differ. Anything else stays literal text.

It is tolerant of partial input (an unclosed fence runs to the end), so drafts can be converted on
every delta. ``rich_blocks`` and ``classic_html`` render the same intermediate tree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ossbot.gatekeeper.telegram.escape import defuse_commands, esc, esc_attr, make_visible
from ossbot.gatekeeper.telegram.ui import blocks as rb
from ossbot.gatekeeper.telegram.ui.sanitize import domain_of

LANG_RE = re.compile(r"^[a-z0-9+#-]{1,20}$")
URL_MAX = 2048
TABLE_COLS_MAX = 20
TABLE_ROWS_MAX = 200
LIST_DEPTH_MAX = 3
QUOTE_EXPAND_LINES = 12
HEADING_SIZE = 2  # every heading level collapses to one section-heading size

_FENCE = re.compile(r"^\s{0,3}(```+|~~~+)([^`]*)$")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
_TASK = re.compile(r"^\[([ xX])\]\s+(.*)$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_INLINE = re.compile(
    r"(?P<code>`[^`\n]+`)"
    r"|(?P<bold>\*\*(?=\S)(?:[^*\n]|\*(?!\*))+?(?<=\S)\*\*)"
    r"|(?P<strike>~~(?=\S)[^~\n]+?(?<=\S)~~)"
    r"|(?P<italic>(?<![\w*])\*(?=\S)[^*\n]+?(?<=\S)\*(?![\w*])|(?<!\w)_(?=\S)[^_\n]+?(?<=\S)_(?!\w))"
    r"|(?P<link>\[(?P<ltext>[^\]\n]{1,500})\]\((?P<url>[^)\s]{1,2100})\))"
)


# ------------------------------------------------------------------------------------ inline
@dataclass(frozen=True)
class Inl:
    kind: str  # t | b | i | s | code | a
    text: str = ""
    children: tuple[Inl, ...] = ()
    url: str = ""


def parse_inline(text: str, depth: int = 0) -> tuple[Inl, ...]:
    out: list[Inl] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append(Inl("t", text[pos : m.start()]))
        pos = m.end()
        tok = m.group(0)
        if m.group("code"):
            out.append(Inl("code", tok[1:-1]))
        elif m.group("link"):
            label, url = m.group("ltext"), m.group("url")
            host = domain_of(url) if len(url) <= URL_MAX else None
            if host is None:
                out.append(Inl("t", tok))  # not http(s): stays literal
            else:
                shown = label if label.strip().lower() == host else f"{label} ({host})"
                out.append(Inl("a", children=(Inl("t", shown),), url=url))
        elif depth >= 2:
            out.append(Inl("t", tok))
        elif m.group("bold"):
            out.append(Inl("b", children=parse_inline(tok[2:-2], depth + 1)))
        elif m.group("strike"):
            out.append(Inl("s", children=parse_inline(tok[2:-2], depth + 1)))
        else:
            out.append(Inl("i", children=parse_inline(tok[1:-1], depth + 1)))
    if pos < len(text):
        out.append(Inl("t", text[pos:]))
    return tuple(out)


# ------------------------------------------------------------------------------------ blocks
@dataclass
class Item:
    text: tuple[Inl, ...]
    checkbox: bool | None = None  # None = no checkbox; else checked
    number: int | None = None
    children: list[Item] = field(default_factory=list)


@dataclass
class Node:
    kind: str  # heading | para | list | pre | table | quote | rule
    text: tuple[Inl, ...] = ()
    items: list[Item] = field(default_factory=list)
    code: str = ""
    language: str | None = None
    rows: list[list[tuple[Inl, ...]]] = field(default_factory=list)
    truncated: bool = False
    quote: list[str] = field(default_factory=list)


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_table_start(lines: list[str], i: int) -> bool:
    return i + 1 < len(lines) and "|" in lines[i] and bool(_TABLE_SEP.match(lines[i + 1])) and "-" in lines[i + 1]


def parse(text: str) -> list[Node]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    nodes: list[Node] = []
    para: list[str] = []
    i = 0

    def flush_para() -> None:
        if para:
            nodes.append(Node("para", text=parse_inline(" ".join(s.strip() for s in para))))
            para.clear()

    while i < len(lines):
        ln = lines[i]
        fence = _FENCE.match(ln)
        if fence:
            flush_para()
            marker, lang = fence.group(1), fence.group(2).strip()
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker[:3]):
                body.append(lines[i])
                i += 1
            i += 1  # the closing fence (or past the end: tolerated)
            nodes.append(Node("pre", code="\n".join(body), language=lang if LANG_RE.match(lang) else None))
            continue
        if not ln.strip():
            flush_para()
            i += 1
            continue
        heading = _HEADING.match(ln)
        if heading:
            flush_para()
            nodes.append(Node("heading", text=parse_inline(heading.group(2))))
            i += 1
            continue
        if _RULE.match(ln):
            flush_para()
            nodes.append(Node("rule"))
            i += 1
            continue
        if _is_table_start(lines, i):
            flush_para()
            header = _cells(ln)[:TABLE_COLS_MAX]
            rows = [header]
            i += 2
            truncated = False
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                if len(rows) > TABLE_ROWS_MAX:
                    truncated = True
                else:
                    row = _cells(lines[i])[:TABLE_COLS_MAX]
                    rows.append(row + [""] * (len(header) - len(row)) if len(row) < len(header) else row[: len(header)])
                i += 1
            nodes.append(Node("table", rows=[[parse_inline(c) for c in r] for r in rows], truncated=truncated))
            continue
        if _QUOTE.match(ln):
            flush_para()
            quoted: list[str] = []
            while i < len(lines) and (m := _QUOTE.match(lines[i])):
                quoted.append(m.group(1))
                i += 1
            nodes.append(Node("quote", quote=quoted))
            continue
        if _LIST.match(ln):
            flush_para()
            items, i = _parse_list(lines, i)
            nodes.append(Node("list", items=items))
            continue
        para.append(ln)
        i += 1
    flush_para()
    return nodes


def _parse_list(lines: list[str], i: int) -> tuple[list[Item], int]:
    root: list[Item] = []
    stack: list[tuple[int, list[Item]]] = [(-1, root)]
    while i < len(lines):
        m = _LIST.match(lines[i])
        if not m:
            if lines[i].strip() and lines[i].startswith("  ") and stack[-1][1]:  # a continuation line
                last = stack[-1][1][-1]
                last.text = (*last.text, Inl("t", " "), *parse_inline(lines[i].strip()))
                i += 1
                continue
            break
        indent = len(m.group(1).replace("\t", "    "))
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        body = m.group(3)
        checkbox: bool | None = None
        task = _TASK.match(body)
        if task:
            checkbox, body = task.group(1) in "xX", task.group(2)
        marker = m.group(2)
        number = int(marker[:-1]) if marker[0].isdigit() else None
        if len(stack) == 1 and root and (root[0].number is None) != (number is None):
            break  # a top-level switch between bullets and numbers starts a new list
        item = Item(parse_inline(body), checkbox, number)
        stack[-1][1].append(item)
        if len(stack) < LIST_DEPTH_MAX:  # deeper items stay in the third level's list
            stack.append((indent, item.children))
        i += 1
    return root, i


# ------------------------------------------------------------------------------------ rich
def _visible(text: str) -> str:
    return make_visible(text)[0]


def _rich_inline(nodes: tuple[Inl, ...]) -> rb.RichText:
    parts: list[rb.RichText] = []
    for n in nodes:
        if n.kind == "t":
            parts.append(_visible(n.text))
        elif n.kind == "code":
            parts.append(rb.RichTextCode(text=_visible(n.text)))
        elif n.kind == "b":
            parts.append(rb.RichTextBold(text=_rich_inline(n.children)))
        elif n.kind == "i":
            parts.append(rb.RichTextItalic(text=_rich_inline(n.children)))
        elif n.kind == "s":
            parts.append(rb.RichTextStrikethrough(text=_rich_inline(n.children)))
        elif n.kind == "a":
            parts.append(rb.RichTextUrl(text=_rich_inline(n.children), url=n.url))
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else parts


def _rich_items(items: list[Item]) -> rb.ListBlock:
    out: list[rb.ListItem] = []
    for it in items:
        blocks: list[rb.Block] = [rb.Paragraph(text=_rich_inline(it.text))]
        if it.children:
            blocks.append(_rich_items(it.children))
        out.append(rb.ListItem(
            blocks=blocks, has_checkbox=True if it.checkbox is not None else None,
            is_checked=True if it.checkbox else None, value=it.number, type="1" if it.number is not None else None,
        ))  # fmt: skip
    return rb.ListBlock(items=out)


def rich_blocks(text: str) -> list[rb.Block]:
    """Already-redacted Markdown -> ``InputRichBlock`` models."""
    out: list[rb.Block] = []
    for n in parse(text):
        if n.kind == "heading":
            out.append(rb.Heading(text=_rich_inline(n.text), size=HEADING_SIZE))
        elif n.kind == "para":
            out.append(rb.Paragraph(text=_rich_inline(n.text)))
        elif n.kind == "rule":
            out.append(rb.Divider())
        elif n.kind == "pre":
            out.append(rb.Pre(text=_visible(n.code), language=n.language))
        elif n.kind == "list" and n.items:
            out.append(_rich_items(n.items))
        elif n.kind == "table":
            cells = [[rb.TableCell(text=_rich_inline(c) or None, is_header=True if r == 0 else None) for c in row]
                     for r, row in enumerate(n.rows)]  # fmt: skip
            out.append(rb.Table(cells=cells, is_bordered=True, is_compact=True))
            if n.truncated:
                out.append(rb.Paragraph(text="…"))
        elif n.kind == "quote":
            if len(n.quote) > QUOTE_EXPAND_LINES:
                texts: list[rb.RichText] = []
                for j, q in enumerate(n.quote):
                    if j:
                        texts.append("\n")
                    texts.append(_rich_inline(parse_inline(q)))
                out.append(rb.ExpandableBlockQuote(text=texts))
            else:
                inner = [rb.Paragraph(text=_rich_inline(parse_inline(q))) for q in n.quote if q.strip()]
                out.append(rb.BlockQuote(blocks=inner or [rb.Paragraph(text=" ")]))
    return out


# ------------------------------------------------------------------------------------ classic
def _leaf(text: str) -> str:
    return esc(defuse_commands(_visible(text)))


def _html_inline(nodes: tuple[Inl, ...]) -> str:
    out: list[str] = []
    for n in nodes:
        if n.kind == "t":
            out.append(_leaf(n.text))
        elif n.kind == "code":
            out.append(f"<code>{esc(_visible(n.text))}</code>")
        elif n.kind == "a":
            out.append(f'<a href="{esc_attr(n.url)}">{_html_inline(n.children)}</a>')
        else:
            tag = {"b": "b", "i": "i", "s": "s"}[n.kind]
            out.append(f"<{tag}>{_html_inline(n.children)}</{tag}>")
    return "".join(out)


def _plain_inline(nodes: tuple[Inl, ...]) -> str:
    return "".join(n.text if n.kind in {"t", "code"} else _plain_inline(n.children) for n in nodes)


def _html_items(items: list[Item], depth: int) -> list[str]:
    out: list[str] = []
    for it in items:
        mark = f"{it.number}." if it.number is not None else "•"
        box = "" if it.checkbox is None else ("☑ " if it.checkbox else "☐ ")
        out.append(f"{'  ' * depth}{mark} {box}{_html_inline(it.text)}")
        out += _html_items(it.children, depth + 1)
    return out


def classic_html(text: str) -> str:
    """Already-redacted Markdown -> classic HTML (tags ``b i s code pre a blockquote`` only)."""
    out: list[str] = []
    for n in parse(text):
        if n.kind == "heading":
            out.append(f"<b>{_html_inline(n.text)}</b>")
        elif n.kind == "para":
            out.append(_html_inline(n.text))
        elif n.kind == "rule":
            out.append("———")
        elif n.kind == "pre":
            body = esc(_visible(n.code))
            out.append(f'<pre><code class="language-{n.language}">{body}</code></pre>' if n.language
                       else f"<pre>{body}</pre>")  # fmt: skip
        elif n.kind == "list":
            out.append("\n".join(_html_items(n.items, 0)))
        elif n.kind == "table":
            plain = [[_plain_inline(c) for c in row] for row in n.rows]
            widths = [max(len(r[c]) for r in plain) for c in range(len(plain[0]))] if plain and plain[0] else []
            rows = ["  ".join(cell.ljust(widths[c]) for c, cell in enumerate(r)).rstrip() for r in plain]
            if n.truncated:
                rows.append("…")
            out.append(f"<pre>{esc(_visible(chr(10).join(rows)))}</pre>")
        elif n.kind == "quote":
            inner = "\n".join(_html_inline(parse_inline(q)) for q in n.quote)
            tag = "blockquote expandable" if len(n.quote) > QUOTE_EXPAND_LINES else "blockquote"
            out.append(f"<{tag}>{inner}</blockquote>")
    return "\n\n".join(p for p in out if p)
