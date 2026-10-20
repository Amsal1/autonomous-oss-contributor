"""demokit.frame: static, script-free pages; deterministic; the template carries only chrome text."""

from __future__ import annotations

import re

import pytest

from demokit import chatstate, checks, frame
from demokit.transcript import Transcript
from tests.unit.devtools.conftest import Tx

CHROME_TEXT = {"9:41", "ob", "ossbot", "Message", "ossbot demo", "Pinned message", "streaming…"}


def _frame(pressed: str | None = None) -> chatstate.Frame:
    tx = Tx()
    tx.bot("createForumTopic", {"name": "✅ Reviews"}, {"message_thread_id": 30})
    mid = tx.send("<b>card</b> &amp; text", markup={"inline_keyboard": [
        [{"text": "✅ Approve", "callback_data": "t:a", "style": "success"},
         {"text": "❌ Reject", "callback_data": "t:b"}],
        [{"text": "🔗 Open PR ↗", "url": "https://example.org"}, {"text": "Off", "disabled": {}}],
        [{"text": "🛑 Kill", "callback_data": "t:k", "style": "danger"}]]})  # fmt: skip
    tx.mark("03-card", [mid], pressed=pressed)
    transcript = Transcript.model_validate(tx.doc())
    return chatstate.snapshot(transcript, transcript.marks()["03-card"])


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("layout", ["still", "gif"])
def test_both_themes_and_layouts_render_faithfully_and_deterministically(theme: str, layout: str) -> None:
    caption = frame.Caption("3 / 9", "Title", "Sub", 2, 9)
    page = frame.render_page(_frame("✅ Approve"), theme=theme, layout=layout, caption=caption)  # type: ignore[arg-type]
    assert page == frame.render_page(_frame("✅ Approve"), theme=theme, layout=layout, caption=caption)  # type: ignore[arg-type]
    assert f'class="{theme} {layout}"' in page
    assert checks.assert_faithful(page, _frame("✅ Approve")) == 1
    assert ('class="cap-title"' in page) is (layout == "gif")


def test_page_is_static_with_a_strict_csp() -> None:
    page = frame.render_page(_frame(), theme="light", layout="still")
    assert "<script" not in page.lower() and "href=" not in page and "src=" not in page
    assert "default-src 'none'; style-src 'unsafe-inline'; img-src data:" in page


def test_keyboard_styles_and_pressed() -> None:
    page = frame.render_page(_frame("✅ Approve"), theme="light", layout="still")
    assert '<div class="btn success pressed">✅ Approve</div>' in page
    assert '<div class="btn url">🔗 Open PR ↗</div>' in page
    assert '<div class="btn disabled">Off</div>' in page and '<div class="btn danger">🛑 Kill</div>' in page


def test_the_template_text_is_only_chrome() -> None:
    template = (frame.TEMPLATES / "frame.html").read_text(encoding="utf-8")
    texts = {t.strip() for t in re.findall(r">([^<>$]+)<", template) if t.strip()}
    assert texts <= CHROME_TEXT, texts - CHROME_TEXT
    page = frame.render_page(_frame(), theme="light", layout="still")
    root = checks.parse(page)
    chrome = {n.text().strip() for n in root.walk() if "data-chrome" in n.attrs}
    assert "Message" in chrome and any("ossbot" in t for t in chrome)
