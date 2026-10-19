"""S3 file view (DESIGN §14.11.3, §14.11.5; AC 5, AC 7, AC 52): one view per (cid, rev) edited in place
while fewer than 20 messages followed it, a new message (and a "moved ↓" stub) after that; quote-replies
anchor comments to lines, an ambiguous quote asks for the line."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from tests.telegram.reviewkit import Card, comments, labels, last_sent_id, latest, open_card, text, token, view_message
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


async def _files(rig: TgRig) -> tuple[Card, int]:
    card = await open_card(rig)
    await rig.tap(token(rig, card.message_id, "🧾 Files"), message=card.message_id)
    return card, view_message(rig.ctx, "file")


async def test_file_view_shows_the_first_file_as_a_diff(rig: TgRig) -> None:
    card, mid = await _files(rig)
    payload = latest(rig, mid)
    assert payload["reply_parameters"]["message_id"] == card.message_id
    shown = text(rig, mid)
    assert "src/app.py</code> · file 1/2 · +2 −1" in shown
    assert '<pre><code class="language-diff">' in shown and "+y = 3" in shown
    assert labels(rig, mid)[:3] == ["◀ Prev file", "📂 1/2", "Next file ▶"]
    assert rig.buttons(payload)[0][0].get("disabled") == {}


async def test_next_file_edits_in_place_below_twenty_messages(rig: TgRig) -> None:
    _, mid = await _files(rig)
    sends = len(rig.sent())
    await rig.tap(token(rig, mid, "Next file ▶"), message=mid)
    assert len(rig.sent()) == sends  # edited, not re-sent
    assert "tests/test_app.py</code> · file 2/2" in text(rig, mid)


async def test_after_twenty_messages_the_view_moves_down(rig: TgRig) -> None:
    ctx = rig.ctx
    _, mid = await _files(rig)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE tg_thread_seq SET n = n + 20")
    await rig.tap(token(rig, mid, "Next file ▶"), message=mid)
    new_mid = view_message(ctx, "file")
    assert new_mid != mid
    assert "tests/test_app.py" in text(rig, new_mid)
    assert text(rig, mid) == "🧾 moved ↓"
    assert db.one(ctx.conn, "SELECT stale FROM tg_views WHERE message_id = ?", (mid,))[0] == 1


async def test_file_picker_lists_every_file(rig: TgRig) -> None:
    _, mid = await _files(rig)
    await rig.tap(token(rig, mid, "📂 1/2"), message=mid)
    assert labels(rig, mid)[:2] == ["1 app.py", "2 test_app.py"]
    await rig.tap(token(rig, mid, "2 test_app.py"), message=mid)
    assert "file 2/2" in text(rig, mid)


async def test_hunk_comment_is_anchored_to_the_hunk(rig: TgRig) -> None:
    card, mid = await _files(rig)
    await rig.tap(token(rig, mid, "💬 Comment"), message=mid)
    await rig.tap(token(rig, mid, "H1"), message=mid)
    prompt = last_sent_id(rig)
    assert latest(rig, prompt)["reply_markup"]["force_reply"] is True
    await rig.say("Rename y", reply_to=prompt)
    (row,) = comments(rig.ctx, card.cid)
    assert (row["path"], row["side"], row["line_start"], row["line_end"]) == ("src/app.py", "new", 2, 3)
    assert row["text"] == "Rename y" and row["rev"] == 1


async def test_quote_reply_anchors_one_line(rig: TgRig) -> None:
    card, mid = await _files(rig)
    await rig.say("Why 3?", reply_to=mid, quote="y = 3")
    (row,) = comments(rig.ctx, card.cid)
    assert (row["path"], row["side"], row["line_start"], row["line_end"]) == ("src/app.py", "new", 3, 3)
    assert row["quote"] == "y = 3"


async def test_removed_lines_anchor_on_the_old_side(rig: TgRig) -> None:
    card, mid = await _files(rig)
    await rig.say("Keep it", reply_to=mid, quote="x = 1")
    (row,) = comments(rig.ctx, card.cid)
    assert (row["side"], row["line_start"]) == ("old", 2)


async def test_ambiguous_quote_asks_for_the_line(rig: TgRig) -> None:
    card, mid = await _files(rig)
    await rig.say("Use logging", reply_to=mid, quote="print")
    assert comments(rig.ctx, card.cid) == []
    picker = last_sent_id(rig)
    assert labels(rig, picker) == ["L4", "L5"]
    await rig.tap(token(rig, picker, "L5"), message=picker)
    (row,) = comments(rig.ctx, card.cid)
    assert (row["line_start"], row["text"]) == (5, "Use logging")


async def test_automatic_quote_is_a_file_level_comment(rig: TgRig) -> None:
    card, mid = await _files(rig)
    await rig.say("General note", reply_to=mid, quote="y = 3", quote_manual=False)
    (row,) = comments(rig.ctx, card.cid)
    assert row["path"] == "src/app.py" and row["line_start"] is None
