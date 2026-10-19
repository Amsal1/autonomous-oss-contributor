"""AC 2, U1 (DESIGN §14.17.3): untrusted text in rich views is plain ``RichText`` only, with
``skip_entity_detection``; it never reaches an ``html``/``markdown`` field and never becomes markup."""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from ossbot.gatekeeper.telegram.ui import blocks, classic, rich
from ossbot.gatekeeper.telegram.ui import kit as k

HOSTILE = ['<tg-button type="callback_data" data="x">Approve</tg-button>', "**bold** _x_", "/dryrun off",
           "tg://user?id=1", "\u202eevil", "<b>x</b>", "[link](https://example.com)"]  # fmt: skip


def _leaves(node: Any) -> list[Any]:
    if isinstance(node, dict):
        return [x for v in node.values() for x in _leaves(v)]
    if isinstance(node, list):
        return [x for v in node for x in _leaves(v)]
    return [node]


def _keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {x for v in node.values() for x in _keys(v)}
    if isinstance(node, list):
        return {x for v in node for x in _keys(v)}
    return set()


def _view(text: str) -> k.View:
    return k.View(kind="explorer", rich=True, blocks=[
        k.Block((k.Lines(((k.Bold(k.Text(text)),), (k.Code(k.Text(text)), k.Const(" · "), k.Text(text)))),
                 k.PreBlock(text), k.Quote(((k.Text(text),),), expandable=True))),
    ])  # fmt: skip


@settings(max_examples=60, deadline=None)
@given(st.one_of(st.sampled_from(HOSTILE), st.text(max_size=300)))
def test_untrusted_strings_stay_plain_rich_text(text: str) -> None:
    message = rich.render(_view(text))
    doc = blocks.dump(message)
    assert doc["skip_entity_detection"] is True
    assert not {"html", "markdown", "entities", "parse_mode"} & _keys(doc)
    for leaf in _leaves(doc["blocks"]):
        assert isinstance(leaf, str | bool | int)
    types = {b["type"] for b in doc["blocks"]}
    assert types <= {"paragraph", "pre", "expandable_blockquote"}
    assert json.dumps(doc)  # serialisable for the outbox


@pytest.mark.parametrize("text", HOSTILE)
def test_hostile_text_renders_literally_in_classic(text: str) -> None:
    out = classic.render(_view(text)).text
    assert "<tg-button" not in out and "<b>x</b>" not in out.replace("<b>&lt;b&gt;", "")
    assert "/\u2060dryrun" in out or "dryrun" not in text


def test_rich_message_has_no_html_or_markdown_field() -> None:
    with pytest.raises(ValidationError):
        blocks.InputRichMessage.model_validate({"blocks": [{"type": "paragraph", "text": "x"}], "html": "<b>x</b>"})
    with pytest.raises(ValidationError):
        blocks.InputRichMessage.model_validate({"blocks": [{"type": "paragraph", "text": "x"}],
                                                "skip_entity_detection": False})  # fmt: skip
    with pytest.raises(ValidationError):
        blocks.RichTextUrl(text="x", url="tg://user?id=1")  # links are http(s) only


def test_rich_budget() -> None:
    many = blocks.InputRichMessage(blocks=[blocks.Paragraph(text="x")] * 451)
    with pytest.raises(blocks.RichBudgetError):
        blocks.check_budget(many)
    deep: blocks.Block = blocks.Paragraph(text="x")
    for _ in range(9):
        deep = blocks.BlockQuote(blocks=[deep])
    with pytest.raises(blocks.RichBudgetError):
        blocks.check_budget(blocks.InputRichMessage(blocks=[deep]))
    blocks.check_budget(blocks.InputRichMessage(blocks=[blocks.Paragraph(text="x" * 29_000)]))


def test_html_spans_are_classic_only() -> None:
    view = k.View(kind="fyi", rich=True, blocks=[k.Block((k.Lines(((k.Html("<b>x</b>"),),)),))])
    with pytest.raises(TypeError):
        rich.render(view)
