"""Bot API 10.2/10.3 rich messages (``rich_message.blocks``) -> display HTML, plus their visible text.

The block and RichText types are those of ``src/ossbot/gatekeeper/telegram/ui/blocks.py``. Every string
is escaped; urls become styled spans (never live links); unknown types fail (fail closed). Blocks are
separated by a newline text node (collapsed by CSS), so a page's text content equals ``plain_text``.
List bullets and numbers are CSS-generated, so they are not text.
"""

from __future__ import annotations

import html
from typing import Any

BLOCKS = frozenset({"paragraph", "heading", "pre", "footer", "divider", "list", "blockquote",
                    "expandable_blockquote", "details", "table", "thinking"})  # fmt: skip
SPANS = {"bold": "b", "italic": "i", "code": "code", "strikethrough": "s"}


class RichError(ValueError):
    pass


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


# ------------------------------------------------------------------------------- RichText
def rt_html(rt: Any) -> str:
    if isinstance(rt, str):
        return _esc(rt)
    if isinstance(rt, list):
        return "".join(rt_html(x) for x in rt)
    if isinstance(rt, dict):
        kind = rt.get("type")
        if kind in SPANS:
            tag = SPANS[str(kind)]
            cls = ' class="tg-code"' if tag == "code" else ""
            return f"<{tag}{cls}>{rt_html(rt.get('text', ''))}</{tag}>"
        if kind == "url":
            url = str(rt.get("url", ""))
            cls = "tg-link" if url.startswith(("https://", "http://")) else "tg-plain"
            return f'<span class="{cls}">{rt_html(rt.get("text", ""))}</span>'
        if kind == "date_time":
            return f'<span class="tg-time">{rt_html(rt.get("text", ""))}</span>'
    raise RichError(f"unknown RichText {rt!r:.80}")


def rt_text(rt: Any) -> str:
    if isinstance(rt, str):
        return rt
    if isinstance(rt, list):
        return "".join(rt_text(x) for x in rt)
    if isinstance(rt, dict) and rt.get("type") in {*SPANS, "url", "date_time"}:
        return rt_text(rt.get("text", ""))
    raise RichError(f"unknown RichText {rt!r:.80}")


# ------------------------------------------------------------------------------- blocks
def _kind(block: Any) -> str:
    if not isinstance(block, dict) or block.get("type") not in BLOCKS:
        raise RichError(f"unknown block {block!r:.80}")
    return str(block["type"])


def _join(parts: list[str]) -> str:
    return "\n".join(parts)


def block_html(block: Any) -> str:
    kind = _kind(block)
    if kind in {"paragraph", "heading", "footer", "thinking", "expandable_blockquote"}:
        cls = {"paragraph": "rb-p", "heading": "rb-h", "footer": "rb-f", "thinking": "rb-think",
               "expandable_blockquote": "rb-q tg-exp"}[kind]  # fmt: skip
        return f'<div class="rb {cls}">{rt_html(block.get("text", ""))}</div>'
    if kind == "pre":
        return f'<div class="rb rb-pre">{rt_html(block.get("text", ""))}</div>'
    if kind == "divider":
        return '<div class="rb rb-hr"></div>'
    if kind == "blockquote":
        return f'<div class="rb rb-q">{blocks_html(block.get("blocks", []))}</div>'
    if kind == "details":
        summary = f'<div class="rb-sum">{rt_html(block.get("summary", ""))}</div>'
        return f'<div class="rb rb-details">{summary}\n{blocks_html(block.get("blocks", []))}</div>'
    if kind == "list":
        items = []
        for item in block.get("items", []):
            number = item.get("value")
            attr = f' data-n="{int(number)}"' if number is not None else ""
            check = " rb-check" if item.get("has_checkbox") else ""
            items.append(f'<div class="rb-li{check}"{attr}>{blocks_html(item.get("blocks", []))}</div>')
        return f'<div class="rb rb-list">{_join(items)}</div>'
    # table: CSS grid of divs (text nodes between <td>s would be moved by the HTML parser)
    rows = []
    for row in block.get("cells", []):
        cells = [f'<div class="rb-td{" rb-th" if c.get("is_header") else ""}">{rt_html(c.get("text") or "")}</div>'
                 for c in row]  # fmt: skip
        rows.append(f'<div class="rb-tr">{chr(9).join(cells)}</div>')
    return f'<div class="rb rb-table">{_join(rows)}</div>'


def blocks_html(blocks: list[Any]) -> str:
    return _join([block_html(b) for b in blocks])


def block_text(block: Any) -> str:
    kind = _kind(block)
    if kind in {"paragraph", "heading", "footer", "thinking", "expandable_blockquote", "pre"}:
        return rt_text(block.get("text", ""))
    if kind == "divider":
        return ""
    if kind == "blockquote":
        return blocks_text(block.get("blocks", []))
    if kind == "details":
        return rt_text(block.get("summary", "")) + "\n" + blocks_text(block.get("blocks", []))
    if kind == "list":
        return _join([blocks_text(item.get("blocks", [])) for item in block.get("items", [])])
    return _join(["\t".join(rt_text(c.get("text") or "") for c in row) for row in block.get("cells", [])])


def blocks_text(blocks: list[Any]) -> str:
    return _join([block_text(b) for b in blocks])


def to_html(rich_message: dict[str, Any]) -> str:
    return blocks_html(list(rich_message.get("blocks", [])))


def plain_text(rich_message: dict[str, Any]) -> str:
    return blocks_text(list(rich_message.get("blocks", [])))
