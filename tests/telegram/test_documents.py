"""Review documents (DESIGN §14.11.4; AC 23): ``📄 .patch``, ``🌐 HTML`` and ``📄 pr_body.md`` carry the
derived bytes unaltered, are scanned first (a hit sends only a fixed note), and the HTML is escaped,
script-free and behind a CSP."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from jinja2 import nodes

from ossbot.gatekeeper import db, review_html
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.review import docs, patchview
from tests.telegram.reviewkit import PATCH, Card, open_card, sent_with, token, view_message, write_patch
from tests.telegram.tgharness import TgRig, make_rig

GHP = "ghp_" + "Q8xk2LmZ9vR4tYp7" + "Wn3bJc6Hd1Fs5Ga0Ke8U"


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


async def _section(rig: TgRig, number: str) -> tuple[Card, int]:
    card = await open_card(rig)
    await rig.tap(token(rig, card.message_id, "🧭 Explore"), message=card.message_id)
    explorer = view_message(rig.ctx, "explorer")
    await rig.tap(token(rig, explorer, "1/9 ▾"), message=explorer)
    await rig.tap(token(rig, explorer, number), message=explorer)
    return card, explorer


def _uploads(rig: TgRig) -> list[tuple[str, bytes]]:
    return [(name, data) for method, name, data in rig.req.files if method == "sendDocument"]


async def test_patch_document_is_the_derived_patch(rig: TgRig) -> None:
    card, explorer = await _section(rig, "6")
    await rig.tap(token(rig, explorer, "📄 .patch"), message=explorer)
    assert "Preparing the document" in str(rig.req.last("answerCallbackQuery").get("text"))
    assert _uploads(rig) == [(f"{card.cid}-r1.patch", PATCH.encode())]
    caption = rig.req.last("sendDocument")["caption"]
    assert caption == f"📄 {card.cid} r1 · gatekeeper-derived patch"


async def test_html_document_is_escaped_and_script_free(rig: TgRig) -> None:
    card, explorer = await _section(rig, "6")
    await rig.tap(token(rig, explorer, "🌐 HTML"), message=explorer)
    ((name, data),) = _uploads(rig)
    assert name == f"ossbot-review-{card.cid}-r1.html"
    page = data.decode()
    assert "Content-Security-Policy" in page and "default-src 'none'" in page
    assert "<script" not in page.lower()
    assert '<tr class="a"><td class="n"></td><td class="n">3</td><td>+y = 3</td></tr>' in page
    assert '<tr class="d"><td class="n">2</td><td class="n"></td><td>-x = 1</td></tr>' in page
    assert "🌐 Full diff · 2 files +4 −1 · r1" in rig.req.last("sendDocument")["caption"]


async def test_pr_body_document_is_the_publish_body(rig: TgRig) -> None:
    card, explorer = await _section(rig, "9")
    await rig.tap(token(rig, explorer, "📄 pr_body.md"), message=explorer)
    body = db.one(rig.ctx.conn, "SELECT pr_body FROM contributions WHERE id = ?", (card.cid,))[0]
    assert _uploads(rig) == [("pr_body.md", body.encode())]


async def test_a_secret_in_the_patch_sends_only_a_note(rig: TgRig) -> None:
    card, explorer = await _section(rig, "6")
    write_patch(rig.ctx, card.cid, 1, PATCH.replace("y = 3", f"y = '{GHP}'"))
    await rig.tap(token(rig, explorer, "📄 .patch"), message=explorer)
    assert _uploads(rig) == []
    assert sent_with(rig, docs.WITHHELD)
    assert all(GHP not in str(p) for _, p in rig.req.calls)


async def test_missing_patch_is_a_note(rig: TgRig) -> None:
    card, explorer = await _section(rig, "6")
    (rig.ctx.gitops.derived_dir(card.cid, 1) / "change.patch").unlink()
    await rig.tap(token(rig, explorer, "📄 .patch"), message=explorer)
    assert _uploads(rig) == [] and sent_with(rig, "Patch unavailable")


def test_template_has_no_safe_filter() -> None:
    env = review_html.environment()
    assert env.autoescape is True
    tree = env.parse(review_html.template_source())
    filters = {f.name for f in tree.find_all(nodes.Filter)}
    assert "safe" not in filters and not list(tree.find_all(nodes.MarkSafe))


def test_render_escapes_markup_in_code_and_text() -> None:
    files = patchview.parse(PATCH.replace("y = 3", "y = '<script>alert(1)</script>'"))
    page = review_html.render(cid="c_X", rev=1, upstream="a/b", facts=["f"], files=files, title="<b>t</b>",
                              body="<img src=x>", commit_message="m").decode()  # fmt: skip
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "&lt;b&gt;t&lt;/b&gt;" in page and "&lt;img src=x&gt;" in page
