"""``chat/md2blocks.py`` (DESIGN §14.12.2, U1): Hermes Markdown -> rich blocks and classic HTML."""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from ossbot.gatekeeper.chat import md2blocks
from ossbot.gatekeeper.telegram.ui import blocks as rb


def _rich(text: str) -> list[dict[str, object]]:
    return [b.model_dump(mode="json", exclude_none=True) for b in md2blocks.rich_blocks(text)]


def test_headings_paragraphs_and_inline_marks() -> None:
    out = _rich("# Title\n### Sub\nSome **bold**, *it*, `code` and ~~gone~~.")
    assert out[0] == {"type": "heading", "text": "Title", "size": 2}
    assert out[1]["size"] == 2  # every level collapses to one size
    text = json.dumps(out[2])
    for kind in ("bold", "italic", "code", "strikethrough"):
        assert f'"type": "{kind}"' in text


def test_lists_nest_three_levels_and_task_items_are_checkboxes() -> None:
    out = _rich("- a\n  - b\n    - c\n      - d\n- [x] done\n- [ ] todo\n1. one\n2. two")
    lst = out[0]
    assert lst["type"] == "list"
    a = lst["items"][0]
    b = a["blocks"][1]["items"][0]
    c_list = b["blocks"][1]["items"]
    assert [i["blocks"][0]["text"] for i in c_list] == ["c", "d"]  # level 4 flattened into level 3
    assert lst["items"][1]["has_checkbox"] is True and lst["items"][1]["is_checked"] is True
    assert "is_checked" not in lst["items"][2]
    assert out[1]["items"][1]["value"] == 2


def test_fences_keep_a_safe_language_and_tolerate_an_unclosed_fence() -> None:
    out = _rich("```python\nprint(1)\n```\n```Bad Lang!\nx\n```\n```diff\n+ partial")
    assert out[0] == {"type": "pre", "text": "print(1)", "language": "python"}
    assert "language" not in out[1]
    assert out[2]["text"] == "+ partial" and out[2]["language"] == "diff"


def test_tables_become_table_blocks_with_a_header_row() -> None:
    out = _rich("| a | b |\n|---|:-:|\n| 1 | **2** |\n| 3 |")
    table = out[0]
    assert table["type"] == "table"
    assert table["cells"][0][0] == {"text": "a", "is_header": True, "align": "left", "valign": "top"}
    assert "text" not in table["cells"][2][1]  # a missing cell is an empty (invisible) cell
    html = md2blocks.classic_html("| a | b |\n|---|---|\n| 1 | 2 |")
    assert html.startswith("<pre>a  b\n1  2</pre>")


def test_long_quotes_are_expandable() -> None:
    short = _rich("> one\n> two")
    assert short[0]["type"] == "blockquote"
    long = _rich("\n".join(f"> line {i}" for i in range(13)))
    assert long[0]["type"] == "expandable_blockquote"


def test_links_only_http_with_the_domain_shown() -> None:
    out = json.dumps(_rich("[docs](https://developer.hashicorp.com/x) [x](tg://resolve?domain=a) "
                           "[y](javascript:alert(1)) <b>raw</b>"))  # fmt: skip
    assert '"url": "https://developer.hashicorp.com/x"' in out and "docs (developer.hashicorp.com)" in out
    assert "tg://" in out and '"url": "tg://' not in out and '"url": "javascript' not in out
    assert "<b>raw</b>" in out  # stays literal text in a plain string leaf


def test_classic_html_is_escaped_and_defused() -> None:
    html = md2blocks.classic_html("Run /kill now <script>x</script> & **go** [a](https://e.org)")
    assert "<script>" not in html and "&lt;script&gt;" in html and "&amp;" in html
    assert "/\u2060kill" in html and "<b>go</b>" in html and '<a href="https://e.org">a (e.org)</a>' in html


@settings(max_examples=80, deadline=None)
@given(st.text(st.sampled_from(list("ab #>-*_`~|[]()1.:\n")), max_size=2000))
def test_any_input_parses_into_valid_blocks(text: str) -> None:
    blocks = md2blocks.rich_blocks(text)
    if blocks:
        rb.InputRichMessage(blocks=blocks)  # the pydantic models accept the output
    html = md2blocks.classic_html(text)
    assert html.count("<blockquote") == html.count("</blockquote>") and html.count("<pre>") == html.count("</pre>")
