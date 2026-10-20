"""Typed builders for Bot API 10.2/10.3 rich message blocks (DESIGN §14.17.3, §14.18.1, U1).

The only code that builds rich payloads. Models mirror the Bot API reference (checked 2026-10-19
against https://core.telegram.org/bots/api, "InputRichBlock*", "RichText*"; ADR-0020 addendum) with
``extra="forbid"``, so a misspelt field fails in tests. Every string here is a plain ``RichText``
string that the rich renderer has already redacted and made visible; ``InputRichMessage`` has no
``html``/``markdown`` field at all and always sets ``skip_entity_detection``, so untrusted text can
never become markup, a link or a bot command (U1).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from ossbot.gatekeeper.telegram.ui.limits import RICH_MAX_BLOCKS, RICH_MAX_CHARS, RICH_MAX_DEPTH


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RichTextBold(_M):
    type: Literal["bold"] = "bold"
    text: RichText


class RichTextItalic(_M):
    type: Literal["italic"] = "italic"
    text: RichText


class RichTextCode(_M):
    type: Literal["code"] = "code"
    text: RichText


class RichTextStrikethrough(_M):
    type: Literal["strikethrough"] = "strikethrough"
    text: RichText


class RichTextUrl(_M):
    type: Literal["url"] = "url"
    text: RichText
    url: Annotated[str, Field(pattern=r"^https?://", max_length=2048)]


class RichTextDateTime(_M):
    type: Literal["date_time"] = "date_time"
    text: RichText
    unix_time: int
    date_time_format: Annotated[str, Field(pattern=r"^(r|w?[dD]?[tT]?)$")]


type RichText = (
    str
    | list[RichText]
    | RichTextBold
    | RichTextItalic
    | RichTextCode
    | RichTextStrikethrough
    | RichTextUrl
    | RichTextDateTime
)


class Paragraph(_M):
    type: Literal["paragraph"] = "paragraph"
    text: RichText


class Heading(_M):
    type: Literal["heading"] = "heading"
    text: RichText
    size: Annotated[int, Field(ge=1, le=6)] = 3


class Pre(_M):
    type: Literal["pre"] = "pre"
    text: RichText
    language: Annotated[str, Field(pattern=r"^[a-z0-9+#-]{1,20}$")] | None = None


class Footer(_M):
    type: Literal["footer"] = "footer"
    text: RichText


class Divider(_M):
    type: Literal["divider"] = "divider"


class ListItem(_M):
    blocks: list[Block]
    has_checkbox: Literal[True] | None = None  # task lists: display only (§14.12.2)
    is_checked: Literal[True] | None = None
    value: Annotated[int, Field(ge=0)] | None = None  # ordered lists: the item number
    type: Literal["1"] | None = None


class ListBlock(_M):
    type: Literal["list"] = "list"
    items: list[ListItem]


class TableCell(_M):
    """``RichBlockTableCell``: ``align`` and ``valign`` are required by the Bot API."""

    text: RichText | None = None
    is_header: Literal[True] | None = None
    align: Literal["left", "center", "right"] = "left"
    valign: Literal["top", "middle", "bottom"] = "top"


class Table(_M):
    type: Literal["table"] = "table"
    cells: list[list[TableCell]]
    is_bordered: Literal[True] | None = None
    is_compact: Literal[True] | None = None


class Thinking(_M):
    """``InputRichBlockThinking``: the "Thinking…" placeholder, valid only in ``sendRichMessageDraft``."""

    type: Literal["thinking"] = "thinking"
    text: RichText


class BlockQuote(_M):
    type: Literal["blockquote"] = "blockquote"
    blocks: list[Block]


class ExpandableBlockQuote(_M):
    """Bot API 10.3: carries ``text`` (RichText), not nested blocks (§14.23 item 16, checked 2026-10-19)."""

    type: Literal["expandable_blockquote"] = "expandable_blockquote"
    text: RichText


class Details(_M):
    type: Literal["details"] = "details"
    summary: RichText
    blocks: list[Block]
    is_open: Literal[True] | None = None


type Block = Annotated[
    Paragraph
    | Heading
    | Pre
    | Footer
    | Divider
    | ListBlock
    | BlockQuote
    | ExpandableBlockQuote
    | Details
    | Table
    | Thinking,
    Field(discriminator="type"),
]


class InputRichMessage(_M):
    blocks: Annotated[list[Block], Field(min_length=1)]
    skip_entity_detection: Literal[True] = True


for _model in (RichTextBold, RichTextItalic, RichTextCode, RichTextStrikethrough, RichTextUrl, RichTextDateTime,
               Paragraph, Heading, Pre, Footer, ListItem, ListBlock, BlockQuote, ExpandableBlockQuote, Details,
               TableCell, Table, Thinking, InputRichMessage):  # fmt: skip
    _model.model_rebuild()


class RichBudgetError(ValueError):
    pass


def text_len(rt: object) -> int:
    if isinstance(rt, str):
        return len(rt)
    if isinstance(rt, list):
        return sum(text_len(x) for x in rt)
    if isinstance(rt, BaseModel):
        return text_len(getattr(rt, "text", ""))
    return 0


def _walk(blocks: list[Block], depth: int) -> tuple[int, int, int]:
    """(blocks, characters, max depth)."""
    count, chars, deepest = 0, 0, depth
    for blk in blocks:
        count += 1
        for attr in ("text", "summary"):
            if hasattr(blk, attr):
                chars += text_len(getattr(blk, attr))
        if isinstance(blk, Table):
            chars += sum(text_len(cell.text) for row in blk.cells for cell in row)
        children: list[Block] = []
        if isinstance(blk, BlockQuote | Details):
            children = list(blk.blocks)
        elif isinstance(blk, ListBlock):
            children = [b for item in blk.items for b in item.blocks]
        if children:
            c, ch, d = _walk(children, depth + 1)
            count, chars, deepest = count + c, chars + ch, max(deepest, d)
    return count, chars, deepest


def measure(blocks: list[Block]) -> tuple[int, int, int]:
    """(blocks, characters, max depth) of a block list, nested blocks included."""
    return _walk(blocks, 1)


def check_budget(message: InputRichMessage) -> None:
    """≤ 450 blocks, ≤ 30,000 characters, ≤ 8 levels (§14.17.4)."""
    count, chars, depth = _walk(list(message.blocks), 1)
    if count > RICH_MAX_BLOCKS or chars > RICH_MAX_CHARS or depth > RICH_MAX_DEPTH:
        raise RichBudgetError(f"rich message over budget: {count} blocks, {chars} chars, depth {depth}")


def dump(message: InputRichMessage) -> dict[str, object]:
    return message.model_dump(mode="json", exclude_none=True)
