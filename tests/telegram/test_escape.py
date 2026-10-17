"""Escaping and verbatim rendering (DESIGN §14.3, AC 14): balanced HTML under a strict whitelist parser."""

from __future__ import annotations

import html
from html.parser import HTMLParser

from hypothesis import given, settings
from hypothesis import strategies as st

from ossbot.common.gitcmd import NumstatEntry
from ossbot.gatekeeper.telegram import cards
from ossbot.gatekeeper.telegram.escape import esc, make_visible, pre

ALLOWED = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler"}
ENTITIES = {"amp", "lt", "gt", "quot"}


class StrictParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED:
            self.errors.append(f"tag {tag}")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(f"unbalanced {tag}")

    def handle_data(self, data: str) -> None:
        if "<" in data or "&" in data:
            self.errors.append(f"raw char in {data[:20]!r}")
        self.text.append(data)

    def handle_entityref(self, name: str) -> None:
        if name not in ENTITIES:
            self.errors.append(f"entity {name}")
        self.text.append(html.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self.errors.append(f"charref {name}")

    def handle_comment(self, data: str) -> None:
        self.errors.append("comment")

    def handle_decl(self, decl: str) -> None:
        self.errors.append("decl")

    def unknown_decl(self, data: str) -> None:
        self.errors.append("unknown decl")


def parse(markup: str) -> StrictParser:
    parser = StrictParser()
    parser.feed(markup)
    parser.close()
    return parser


@settings(max_examples=300, deadline=None)
@given(st.text())
def test_escaped_text_is_balanced_whitelisted_html(text: str) -> None:
    parser = parse(f"<b>{esc(text)}</b>{pre(text)}")
    assert parser.errors == []
    assert parser.stack == []
    visible, _ = make_visible(text)
    assert "".join(parser.text) == text + visible


@settings(max_examples=150, deadline=None)
@given(st.text(min_size=0, max_size=12000))
def test_verbatim_parts_chunked_and_balanced(text: str) -> None:
    parts = cards.verbatim_parts("PR body", text)
    shown = []
    for part in parts:
        assert len(part) <= cards.MESSAGE_MAX
        parser = parse(part)
        assert parser.errors == []
        assert parser.stack == []
        shown.append("".join(parser.text).split("\n", 1)[1])  # drop the "PR body (i/K)" header
    assert "".join(shown) == make_visible(text)[0]


@settings(max_examples=100, deadline=None)
@given(title=st.text(max_size=300), body=st.text(max_size=5000), path=st.text(min_size=1, max_size=200))
def test_card_is_balanced(title: str, body: str, path: str) -> None:
    view = cards.CardView(
        kind="open_pr", cid="c_0000000000", upstream="langfuse/langfuse", dry_run=True, title=title, body=body,
        commit_message="fix: x\n\nSigned-off-by: A <a@b.c>\n", numstat=(NumstatEntry(1, 2, path),),
        head_sha="a" * 40, round_base="b" * 40, base_ref="main", identity="A <a@b.c>", branch="ossbot/1-x-abc",
        fork_owner="test-bot-login",
    )  # fmt: skip
    rendered = cards.render(view)
    for message in [rendered.card, *rendered.parts]:
        assert len(message) <= 4096
        parser = parse(message)
        assert parser.errors == []
        assert parser.stack == []


def test_hidden_characters_made_visible() -> None:
    text = "a\u200bb\u202ec\ufeffd\x07e"
    visible, count = make_visible(text)
    assert count == 4
    assert visible == "a⟦U+200B⟧b⟦U+202E⟧c⟦U+FEFF⟧d⟦U+0007⟧e"
    assert pre("<!-- hidden --> <details>") == "<pre>&lt;!-- hidden --&gt; &lt;details&gt;</pre>"
    view = cards.CardView(kind="comment", cid="c_0000000000", upstream="a/b", dry_run=False, body=text, target_number=3)
    assert "4 invisible/bidi/control" in cards.render(view).card
