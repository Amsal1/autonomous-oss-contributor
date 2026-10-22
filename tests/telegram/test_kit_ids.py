"""Copyable ids (DESIGN §14.17.3, §14.18.1; post-v1 OD-C1 core): ``k.Id`` renders classic ``<code>`` and
rich ``RichTextCode``; gatekeeper kinds are grammar-checked; branches are untrusted (redacted, escaped);
``autocode`` escapes plain text and wraps only strong id patterns."""

from __future__ import annotations

import pytest

from ossbot.gatekeeper.telegram.ui import blocks as rb
from ossbot.gatekeeper.telegram.ui import classic, ids, rich
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui.limits import LimitError

CID = "c_0123456789"
SHA = "0123456789abcdef0123456789abcdef01234567"
TOKEN = "ghp_" + "A" * 36  # a GitHub token pattern the default scanner redacts


def _classic(*spans: k.Span) -> str:
    return classic.render(k.plain("fyi", tuple(spans))).text


def test_classic_and_rich_render_code() -> None:
    assert _classic(k.Const("cid "), k.Id(CID)) == f"cid <code>{CID}</code>"
    message = rich.render(k.View(kind="fyi", blocks=[k.Block((k.Lines(((k.Id(CID),),)),))], rich=True))
    paragraph = message.blocks[0]
    assert isinstance(paragraph, rb.Paragraph)
    assert isinstance(paragraph.text, rb.RichTextCode) and paragraph.text.text == CID


@pytest.mark.parametrize(("value", "kind"), [(CID, "cid"), ("a_0123456789", "approval"), ("x_0123456789", "action"),
                                             ("git-helper", "skill"), (SHA, "sha"), ("#42", "pr"),
                                             ("h_0123456789", "hold")])  # fmt: skip
def test_kinds_accept_their_grammar(value: str, kind: k.IdKind) -> None:
    assert ids.html(value, kind) == f"<code>{value}</code>"


@pytest.mark.parametrize(("value", "kind"), [("c_01234", "cid"), ("a_0123456789", "cid"), ("c_0123456789<b>", "cid"),
                                             ("Not_A_Sha", "sha"), ("42", "pr"), ("UPPER", "skill")])  # fmt: skip
def test_a_bad_id_is_a_bug(value: str, kind: k.IdKind) -> None:
    with pytest.raises(LimitError):
        ids.html(value, kind)
    with pytest.raises(LimitError):
        _classic(k.Id(value, kind))


def test_branch_is_untrusted() -> None:
    assert _classic(k.Id("feat/<x>&y", "branch")) == "<code>feat/&lt;x&gt;&amp;y</code>"
    shown = _classic(k.Id(f"leak-{TOKEN}", "branch"))
    assert TOKEN not in shown and shown.startswith("<code>") and "REDACTED" in shown


def test_autocode_wraps_only_strong_ids() -> None:
    text = f"Done {CID} at {SHA} <b> & xc_0123456789 c_01234567890 a_0123456789."
    assert ids.autocode(text) == (f"Done <code>{CID}</code> at <code>{SHA}</code> &lt;b&gt; &amp; xc_0123456789"
                                  " c_01234567890 <code>a_0123456789</code>.")  # fmt: skip


def test_spans_split_template_text() -> None:
    assert ids.spans(f"card {CID} r2") == (k.Const("card "), k.Id(CID, "cid"), k.Const(" r2"))
    assert ids.spans("no ids") == (k.Const("no ids"),)


def test_verbatim_is_never_touched() -> None:
    assert _classic(k.Verbatim(f"Fix {CID}")) == f"Fix {CID}"
