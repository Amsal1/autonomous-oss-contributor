"""demokit.richhtml: every rich block type renders escaped; unknown types fail closed."""

from __future__ import annotations

from typing import Any

import pytest

from demokit import richhtml
from demokit.checks import parse

ALL: list[dict[str, Any]] = [
    {"type": "paragraph", "text": ["a ", {"type": "bold", "text": "b"}, {"type": "italic", "text": "i"},
                                   {"type": "code", "text": "<c>"}, {"type": "strikethrough", "text": "s"},
                                   {"type": "url", "text": "u", "url": "https://example.org"},
                                   {"type": "date_time", "text": "now", "unix_time": 1, "date_time_format": "r"}]},
    {"type": "heading", "text": "H", "size": 2},
    {"type": "pre", "text": "x = 1\ny = 2"},
    {"type": "footer", "text": "f"},
    {"type": "divider"},
    {"type": "list", "items": [{"blocks": [{"type": "paragraph", "text": "one"}], "value": 1},
                               {"blocks": [{"type": "paragraph", "text": "two"}], "has_checkbox": True}]},
    {"type": "blockquote", "blocks": [{"type": "paragraph", "text": "q1"}, {"type": "paragraph", "text": "q2"}]},
    {"type": "expandable_blockquote", "text": "e"},
    {"type": "details", "summary": "sum", "blocks": [{"type": "paragraph", "text": "d"}]},
    {"type": "table", "cells": [[{"text": "k", "is_header": True}, {"text": "v"}], [{"text": "1"}, {}]]},
    {"type": "thinking", "text": "Thinking…"},
]  # fmt: skip


def test_every_block_renders_and_its_text_content_equals_plain_text() -> None:
    message = {"blocks": ALL}
    out = richhtml.to_html(message)
    assert "&lt;c&gt;" in out and "<c>" not in out and "href" not in out
    text = richhtml.plain_text(message)
    assert text == "a bi<c>sunow\nH\nx = 1\ny = 2\nf\n\none\ntwo\nq1\nq2\ne\nsum\nd\nk\tv\n1\t\nThinking…"
    assert parse(f"<div>{out}</div>").text() == text  # what a browser's textContent would be


@pytest.mark.parametrize(
    "bad", [{"type": "image", "url": "x"}, "text", {"type": "paragraph", "text": {"type": "spoiler"}}]
)
def test_unknown_types_fail_closed(bad: Any) -> None:
    with pytest.raises(richhtml.RichError):
        richhtml.to_html({"blocks": [bad]})
