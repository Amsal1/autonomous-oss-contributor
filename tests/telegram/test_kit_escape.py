"""U8, AC 3, AC 37 (DESIGN §14.9, §14.17.3): command defusing, undefuse, classic escaping in the kit."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from ossbot.gatekeeper.telegram.escape import WORD_JOINER, defuse_commands, make_visible, tg_time
from ossbot.gatekeeper.telegram.ui import classic
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui.sanitize import label_from_untrusted, undefuse


def _render(*spans: k.Span) -> str:
    return classic.render(k.View(kind="fyi", blocks=[k.Block((k.Lines((tuple(spans),)),))])).text


def test_defuse_puts_a_word_joiner_after_command_slashes() -> None:
    assert defuse_commands("tap /kill or /dryrun off") == f"tap /{WORD_JOINER}kill or /{WORD_JOINER}dryrun off"
    # only a slash followed by a letter can start a command link; digits and doubled slashes stay as they are
    assert defuse_commands("1/2 and // and /9") == "1/2 and // and /9"
    assert defuse_commands("a/b") == f"a/{WORD_JOINER}b"


def test_untrusted_text_is_defused_and_escaped_but_const_is_not() -> None:
    out = _render(k.Text("<b>run /kill</b>"), k.Const(" · use /resume"))
    assert f"&lt;b&gt;run /{WORD_JOINER}kill&lt;/{WORD_JOINER}b&gt;" in out  # every "/<letter>" (U8)
    assert out.endswith(" · use /resume")  # our own hints stay tappable


def test_verbatim_bytes_are_never_defused_or_redacted() -> None:
    title = "Fix /kill handling <script> & \u202e bidi"
    out = _render(k.Verbatim(title, "pr_title"))
    assert WORD_JOINER not in out
    assert "/kill" in out and "&lt;script&gt;" in out and "⟦U+202E⟧" in out


def test_pre_block_bytes_are_identical_after_unescape() -> None:
    import html

    body = "line 1 /kill\n<!-- hidden -->\n& ampersand"
    view = k.View(kind="body_part", blocks=[k.Block((k.PreBlock(body, verbatim=True),))])
    out = classic.render(view).text
    assert out.startswith("<pre>") and out.endswith("</pre>")
    assert html.unescape(out[5:-6]) == body  # AC 3: byte-identical to the stored value


def test_undefuse_removes_only_our_marker() -> None:
    defused = defuse_commands("/kill now")
    assert undefuse(defused) == "/kill now"
    foreign = f"a{WORD_JOINER}b /{WORD_JOINER}x"
    assert undefuse(foreign) == f"a{WORD_JOINER}b /x"
    visible, hidden = make_visible(undefuse(foreign))
    assert hidden == 1 and "⟦U+2060⟧" in visible  # a foreign joiner is still shown (AC 37)


@given(st.text(max_size=200))
def test_undefuse_inverts_defuse(text: str) -> None:
    clean = text.replace(WORD_JOINER, "")
    assert undefuse(defuse_commands(clean)) == clean


def test_tg_time_formats() -> None:
    assert tg_time(1_792_238_400, "12:00") == '<tg-time unix="1792238400" format="r">12:00</tg-time>'
    assert tg_time(1, "x", "wDT").endswith('format="wDT">x</tg-time>')
    for bad in ("rd", "x", "dd"):
        try:
            tg_time(1, "x", bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_labels_from_untrusted_text_are_dropped_not_cut() -> None:
    assert label_from_untrusted("Explain  the\nfix") == "Explain the fix"
    assert label_from_untrusted("/kill") is None
    assert label_from_untrusted("Approve this PR") is None  # no approval words (U2)
    assert label_from_untrusted("x\u200by") is None  # hidden characters
    assert label_from_untrusted("a" * 65) is None  # dropped, never cut
