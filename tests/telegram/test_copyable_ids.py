"""Copyable ids (DESIGN §14.17.3, post-v1 OD-C3): every contribution, approval, proposal, action and hold
id the bot shows is one tap to copy (classic ``<code>``, rich ``code`` node), rendered only through
``telegram/ui/ids.py``.

The lint itself is ``tests/telegram/idcheck.py``. It runs on every Bot API call of every ``TgRig`` test
(``tgharness.lint_calls``: the stale, queue, hold, ceiling, rescope, dashboard, status and digest suites
all go through it) and over the whole demo transcript (``tests/e2e/test_demo_capture.py``). This file
scans the goldens, pins the kit rules, proves the lint fails a bare id, and checks that the extra markup
keeps the length budgets.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest

from ossbot.common.ids import new_id
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import notify as tg_notify
from ossbot.gatekeeper.telegram.review import queue, stale
from ossbot.gatekeeper.telegram.ui import blocks, classic, ids, rich
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send
from ossbot.gatekeeper.telegram.ui.limits import CLASSIC_MAX, LimitError
from tests.telegram import idcheck
from tests.telegram.tgharness import TgRig, make_rig

GOLDEN = Path(__file__).parent / "golden"
# goldens pin per-run cids as a placeholder; scan them as a real id
PLACEHOLDER = re.compile(r"\bc_CID\b|\bCID\b")


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def _golden_hits(path: Path) -> list[str]:
    text = PLACEHOLDER.sub("c_0000000000", path.read_text(encoding="utf-8"))
    if path.suffix == ".json":  # a rich message dump
        return idcheck.bare_ids_rich(json.loads(text))
    if path.suffix == ".html":  # classic HTML; card goldens append the (exempt) keyboard labels after "--"
        return idcheck.bare_ids_html(text.split("\n--\n", 1)[0])
    hits: list[str] = []
    for line in text.splitlines():  # .txt: one recorded [method, payload] call per line, or plain lines
        try:
            call = json.loads(line)
        except ValueError:
            call = None
        if isinstance(call, list) and len(call) == 2 and isinstance(call[1], dict):
            hits += idcheck.bare_ids(str(call[0]), call[1])
        else:
            hits += idcheck.bare_ids_html(line)
    return hits


@pytest.mark.parametrize("path", sorted(p for p in GOLDEN.iterdir() if p.is_file()), ids=lambda p: p.name)
def test_every_golden_shows_its_ids_as_code(path: Path) -> None:
    assert _golden_hits(path) == []


def test_the_golden_scan_sees_a_bare_id(tmp_path: Path) -> None:
    bad = tmp_path / "bad.html"
    bad.write_text("⚪ part of superseded c_0123456789 r1\n--\n⛔ Not approvable\n", encoding="utf-8")
    assert _golden_hits(bad) == ["c_0123456789"]


def test_template_ids_become_code_in_classic_and_rich() -> None:
    cid, aid = new_id("c"), new_id("a")
    view = k.plain("fyi", k.line(f"Re-sent {cid} (card {aid}) · PR #12 · issue #7"),
                   (k.Bold(k.Const(f"{cid} r2")), k.Const(" · "), k.Code(k.Const(cid))))  # fmt: skip
    html = classic.render(view).text
    assert f"Re-sent <code>{cid}</code> (card <code>{aid}</code>) · PR <code>#12</code> · issue #7" in html
    assert f"<b><code>{cid}</code> r2</b> · <code>{cid}</code>" in html  # never nested inside a code span
    assert "<code><code>" not in html and idcheck.bare_ids_html(html) == []
    doc = blocks.dump(rich.render(view))
    assert idcheck.bare_ids_rich(doc) == []
    assert {"type": "code", "text": cid} in doc["blocks"][0]["text"]
    # a link's text is left alone (a tap opens the link; Telegram can't nest code in it)
    link = classic.render(k.plain("fyi", (k.Link(k.Const("PR #12"), "https://github.com/acme/widgets/pull/12"),)))
    assert link.text == '<a href="https://github.com/acme/widgets/pull/12">PR #12</a>'


def test_untrusted_and_publish_bytes_are_never_rewritten() -> None:
    """``Text`` and ``Verbatim`` keep their bytes: an id inside them stays plain (and the lint flags it,
    so a render site must use ``k.Id``)."""
    cid = new_id("c")
    view = k.plain("fyi", (k.Text(f"model says {cid}"),), (k.Verbatim(f"title {cid}", "pr_title"),))
    html = classic.render(view).text
    assert "<code>" not in html and idcheck.bare_ids_html(html) == [cid, cid]


def test_ids_are_escaped_checked_and_branches_redacted() -> None:
    assert ids.autocode("a <b> & c_0123456789") == "a &lt;b&gt; &amp; <code>c_0123456789</code>"
    with pytest.raises(LimitError):  # gatekeeper ids are checked against their grammar: a mismatch is a bug
        ids.html("c_<script>", "cid")
    branch = classic.render(k.plain("fyi", (k.Id("fix/<b>", "branch"),))).text
    assert branch == "<code>fix/&lt;b&gt;</code>"


async def test_the_harness_lint_fails_a_bare_id(rig: TgRig) -> None:
    """The sanity check of B3: a view that shows a cid outside ``k.Id`` fails every TgRig test."""
    cid = new_id("c")
    ui_send.deliver(rig.ctx, k.plain("fyi", (k.Text(f"bare {cid}"),)))
    with pytest.raises(AssertionError, match=r"ids rendered outside ui/ids\.py"):
        await rig.flush()
    ui_send.deliver(rig.ctx, k.plain("fyi", k.line(f"coded {cid}")))
    await rig.flush()  # the same text as template text passes


def test_notify_sends_escaped_html_with_code(ctx: AppContext) -> None:
    cid = new_id("c")
    text = f"🚨 <b>not markup</b> {cid} & more"
    ctx.notify(text)
    row = ctx.conn.execute("SELECT payload_json, priority FROM tg_outbox ORDER BY id DESC LIMIT 1").fetchone()
    payload = json.loads(row["payload_json"])
    assert payload["parse_mode"] == "HTML"
    assert payload["text"] == f"🚨 &lt;b&gt;not markup&lt;/b&gt; <code>{cid}</code> &amp; more"
    assert tg_notify.plain_text(payload) == text  # the merge and the away summary read the shown text
    assert int(row["priority"]) == 0  # classify still reads the raw text (🚨 is P0)


def test_stale_card_with_code_still_fits_its_budget() -> None:
    cid = new_id("c")
    card = "\n".join([f"🧩 <b>Ready for review</b> · <code>{cid}</code> · r1", *(["x" * 120] * 60)])
    out = stale.render(card_html=card, status="invalidated", reason="config_snapshot_changed", cid=cid, rev=1,
                       action_id=new_id("x"), forward="show_current")  # fmt: skip
    head = out.text.split("\n", 1)[0]
    assert head == f"⚪ <b>Invalidated</b> · config reloaded · <code>{cid}</code>"
    assert len(out.text) <= stale.STALE_MAX and idcheck.bare_ids_html(out.text) == []
    stub = stale.part_stub(cid, 2)
    assert stub == f"⚪ part of superseded <code>{cid}</code> r2"


def test_queue_page_with_cids_keeps_its_budget(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """Five items with the longest labels: ≤ 1,500 characters and each item line ≤ 120 shown characters."""
    when = ctx.clock.now() + timedelta(hours=10)
    label = "🧩 PR preview · " + "o" * 40 + "/" + "r" * 40 + " · 👤 · no live card"
    items = [queue.Item(2, 3, when, 0, "t", "a", new_id("c"), label) for _ in range(queue.PER_PAGE)]
    monkeypatch.setattr(queue, "ordered", lambda _ctx, _sort="priority": items)
    html = classic.render(queue.view(ctx)).text
    assert len(html) <= 1500 and idcheck.bare_ids_html(html) == []
    for line in html.split("\n")[1:]:
        shown = re.sub(r"<tg-time[^>]*>([^<]*)</tg-time>", r"\1", line)
        assert len(tg_notify.plain_text({"text": shown, "parse_mode": "HTML"})) <= 120, line
    assert all(f"<code>{i.subject}</code>" in html for i in items)


def test_merged_fyis_with_cids_fit_one_message() -> None:
    """The 📈 N updates merge of 15 FYIs: the lines and the full expandable quote, every id as code, fit
    one message without dropping the quote."""
    texts = [f"ℹ️ Stage validate passed for {new_id('c')} · " + "y" * 40 for _ in range(15)]
    lines: list[k.Line] = [ids.spans(f"• {tg_notify.first_line(t)}") for t in texts[: tg_notify.MERGED_LINES]]
    body = "\n\n".join(texts)[: tg_notify.MERGED_BODY_MAX]
    view = k.View(kind="fyi", blocks=[k.Block((k.Lines(tuple(lines)),), name="lines"),
                                      k.Block((k.Quote(tuple(ids.spans(x) for x in body.split("\n")),
                                                       expandable=True),), required=False, priority=1)])  # fmt: skip
    out = classic.render(view)
    assert out.dropped == () and len(out.text) <= CLASSIC_MAX
    assert out.text.count("<code>c_") == 30 and idcheck.bare_ids_html(out.text) == []
