"""Budgets and keyboard rules (DESIGN §14.17.2, §14.17.4, §14.18.1; AC 1, BA-7).

Every view builder renders within its budget in classic and rich mode with random untrusted input;
every ``callback_data`` is exactly 24 bytes; labels follow the per-row rule; every ForceReply
placeholder constant is 1-60 characters. Item 45-47 builders are added to ``BUILDERS`` as they land.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import chat_relay
from ossbot.gatekeeper.telegram.ui import blocks, classic, rich
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send
from ossbot.gatekeeper.telegram.ui.limits import (
    CLASSIC_MAX,
    PLACEHOLDER_MAX,
    LimitError,
    check_placeholder,
    check_row,
    chunk_verbatim,
    escaped_len,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "ossbot" / "gatekeeper" / "telegram"
untrusted = st.text(st.characters(codec="utf-8"), max_size=6000)


def _answer(text: str) -> k.View:
    """The longest single chunk the relay ever renders (``chat_relay.chunks``)."""
    part = chat_relay.chunks(text)[0]
    return k.View(kind="chat_answer", rich=True, blocks=chat_relay.quoted_answer(chat_relay.HEADER, part))


def _card_like(text: str) -> k.View:
    head = k.header("🧩", "Ready for review", "c_0123456789", k.mode_badge(True))
    blocks = k.card(head, [(k.Text(text, 300),)], [k.fact("Expires", "in 3 d")],
                    [k.Block((k.claim("Planner", text),), required=False, priority=1, name="claim"),
                     k.Block((k.PreBlock(text),), required=False, priority=0, name="details")])  # fmt: skip
    return k.View(kind="card", blocks=blocks)


BUILDERS: dict[str, Callable[[str], k.View]] = {"chat_answer_quoted": _answer, "card_like": _card_like}


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@pytest.mark.parametrize("name", list(BUILDERS))
@given(text=untrusted)
def test_every_builder_fits_its_budget(name: str, text: str) -> None:
    view = BUILDERS[name](text)
    out = classic.render(view)
    assert len(out.text) <= CLASSIC_MAX
    assert out.text.count("<blockquote") == out.text.count("</blockquote>")
    if view.rich:
        blocks.check_budget(rich.render(view))


def test_required_blocks_are_never_dropped() -> None:
    view = _card_like("x" * 5000)
    out = classic.render(view)
    assert "Ready for review" in out.text and "Expires" in out.text
    assert "details" in out.dropped


def test_too_large_required_set_is_a_bug() -> None:
    view = k.View(kind="card", blocks=[k.Block((k.Lines(((k.Const("x" * 4000),),)),))])
    with pytest.raises(k.ViewTooLarge):
        classic.render(view)


@pytest.mark.parametrize(("labels", "ok"), [
    (["a" * 24, "b" * 24], True), (["a" * 25, "b"], False), (["a" * 64], True), (["a" * 65], False),
    (["a" * 13, "b", "c"], False), (["abc", "def", "ghi"], True), (["a", "b", "c", "d"], False),
])  # fmt: skip
def test_label_rule_per_row(labels: list[str], ok: bool) -> None:
    if ok:
        check_row(labels)
    else:
        with pytest.raises(LimitError):
            check_row(labels)


def test_callback_data_is_24_bytes(ctx: AppContext) -> None:
    view = k.plain("picker", k.line("x"), rows=[[k.button("One", "dash_refresh"), k.button("Two", "dash_queue")]])
    with db.transaction(ctx.conn):
        tokens = ui_send.mint(ctx, view, chat_id=4242, message_id=None)
    markup = ui_send.keyboard(view, tokens)
    assert markup is not None
    for row in markup["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"].encode()) == 24


def _placeholder_constants() -> list[str]:
    """Every string passed as ``placeholder=`` or assigned to a ``*PLACEHOLDER*`` name in the telegram package."""
    found: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "placeholder" and isinstance(node.value, ast.Constant):
                found.append(str(node.value.value))
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(
                    node.value.value, str):  # fmt: skip
                for target in node.targets:
                    if isinstance(target, ast.Name) and "PLACEHOLDER" in target.id:
                        found.append(node.value.value)
    return found


def test_placeholder_constants_fit() -> None:
    for text in _placeholder_constants():
        assert check_placeholder(text) == text and len(text) <= PLACEHOLDER_MAX


@given(st.text(max_size=8000))
def test_verbatim_chunks_join_back_and_fit(text: str) -> None:
    chunks = chunk_verbatim(text, 500)
    assert "".join(chunks) == text
    assert all(escaped_len(c) <= 500 for c in chunks)
