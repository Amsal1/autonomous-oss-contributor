"""demokit.tghtml: Telegram classic HTML -> escaped display HTML, fail closed on unknown tags."""

from __future__ import annotations

import pytest

from demokit import tghtml


def test_text_is_re_escaped_and_entities_decoded() -> None:
    src = "<b>a &amp; b</b> &lt;script&gt;alert(1)&lt;/script&gt;"
    out = tghtml.to_html(src)
    assert out == "<b>a &amp; b</b> &lt;script&gt;alert(1)&lt;/script&gt;"
    assert "<script" not in out
    assert tghtml.plain_text(src) == "a & b <script>alert(1)</script>"


def test_allowlisted_tags_map_to_display_elements() -> None:
    src = ('<i>i</i><s>s</s><u>u</u><code>c</code><pre><code class="language-py">x = 1\n</code></pre>'
           "<blockquote expandable>q</blockquote><blockquote>r</blockquote>")  # fmt: skip
    out = tghtml.to_html(src)
    assert '<span class="tg-pre"><code class="tg-code">x = 1\n</code></span>' in out
    assert '<span class="tg-quote tg-exp">q</span>' in out and '<span class="tg-quote">r</span>' in out
    assert tghtml.plain_text(src) == "isucx = 1\nqr"


@pytest.mark.parametrize("tag", ["<script>x</script>", "<img src=x>", "<div>x</div>", "<span>x</span>", "<br/>"])
def test_unknown_tags_fail_closed(tag: str) -> None:
    with pytest.raises(tghtml.TelegramHtmlError):
        tghtml.to_html(tag)


def test_unbalanced_and_comments_fail() -> None:
    for bad in ("<b>x", "<b>x</i>", "<!-- c -->"):
        with pytest.raises(tghtml.TelegramHtmlError):
            tghtml.to_html(bad)


def test_links_are_never_live_and_only_http_is_styled() -> None:
    out = tghtml.to_html('<a href="https://example.org/x">#1</a> <a href="tg://user?id=1">u</a>')
    assert "href" not in out
    assert '<span class="tg-link">#1</span>' in out and '<span class="tg-plain">u</span>' in out


def test_tg_time_shows_its_fallback() -> None:
    src = '⏳ <tg-time unix="1792497620" format="r">20 Oct 12:00</tg-time>'
    assert tghtml.plain_text(src) == "⏳ 20 Oct 12:00"
    assert '<span class="tg-time">20 Oct 12:00</span>' in tghtml.to_html(src)
