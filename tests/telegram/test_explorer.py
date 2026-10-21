"""S2 explorer (DESIGN §14.11.2; AC 4, AC 44): a reply to the card, 9 sections, gatekeeper data only,
no approval token on it (U3), navigation tokens that keep working."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from tests.telegram.reviewkit import Card, labels, latest, open_card, text, token, view_message
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


async def _explore(rig: TgRig) -> tuple[Card, int]:
    card = await open_card(rig)
    await rig.tap(token(rig, card.message_id, "🧭 Explore"), message=card.message_id)
    return card, view_message(rig.ctx, "explorer")


async def test_card_keyboard_per_open_pr(rig: TgRig) -> None:
    card = await open_card(rig)
    assert labels(rig, card.message_id) == ["✅ Approve & Submit", "❌ Reject", "🧭 Explore", "🧾 Files (2)",
                                            "✏️ Request changes", "📝 Edit text"]  # fmt: skip
    approve = rig.buttons(latest(rig, card.message_id))[0][0]
    assert approve.get("style") == "success"


async def test_explorer_is_a_reply_to_the_card_with_no_decision_token(rig: TgRig) -> None:
    card, mid = await _explore(rig)
    payload = latest(rig, mid)
    assert payload["reply_parameters"]["message_id"] == card.message_id
    shown = text(rig, mid)
    assert f"{card.cid} r1" in shown and "1/9 Summary" in shown
    assert "2 files +4 −1" in shown
    assert not any("Approve" in label or "Confirm" in label for label in labels(rig, mid))
    for row in rig.buttons(payload):
        for button in row:
            assert len(str(button.get("callback_data", "")).encode()) <= 64
    # every explorer token is a kit token: no approval id, bound to the view message (delta 1)
    rows = rig.ctx.conn.execute("SELECT approval_id, arg FROM ui_tokens WHERE purpose = 'nav'"
                                " AND tg_message_id = ?", (mid,)).fetchall()  # fmt: skip
    assert rows and all(r["approval_id"] is None for r in rows)
    assert all(json.loads(r["arg"])["action_id"] == card.action for r in rows)


async def test_section_pager_and_picker(rig: TgRig) -> None:
    _, mid = await _explore(rig)
    await rig.tap(token(rig, mid, "▶"), message=mid)
    assert "2/9 Why this issue" in text(rig, mid)
    await rig.tap(token(rig, mid, "2/9 ▾"), message=mid)
    assert labels(rig, mid)[:9] == [str(i) for i in range(1, 10)]
    await rig.tap(token(rig, mid, "6"), message=mid)
    shown = text(rig, mid)
    assert "6/9 Diff" in shown and "src/app.py" in shown and "-1,4 +1,5" in shown
    assert "📄 .patch" in labels(rig, mid) and "🌐 HTML" in labels(rig, mid)


async def test_files_section_opens_the_file_view(rig: TgRig) -> None:
    _, mid = await _explore(rig)
    await rig.tap(token(rig, mid, "🧾 Files"), message=mid)
    assert "5/9 Files" in text(rig, mid)
    await rig.tap(token(rig, mid, "Open 2"), message=mid)
    assert "tests/test_app.py</code> · file 2/2" in text(rig, view_message(rig.ctx, "file"))


async def test_pr_text_section_shows_the_title_verbatim(rig: TgRig) -> None:
    card, mid = await _explore(rig)
    with db.transaction(rig.ctx.conn):
        rig.ctx.conn.execute("UPDATE contributions SET pr_title = ? WHERE id = ?", ("Fix <b>&amp; thing", card.cid))
    await rig.tap(token(rig, mid, "1/9 ▾"), message=mid)
    await rig.tap(token(rig, mid, "9"), message=mid)
    shown = text(rig, mid)
    assert "<code>Fix &lt;b&gt;&amp;amp; thing</code>" in shown
    assert "Disclosure ✅" in shown


async def test_summary_says_whether_the_commit_is_signed(rig: TgRig) -> None:
    """ADR-0006 addendum: section 1 reads the signature from the gatekeeper's derived meta.json."""
    card, mid = await _explore(rig)
    assert "⚠️ Commit not signed" in text(rig, mid)
    meta_path = rig.ctx.gitops.derived_dir(card.cid, 1) / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps({**meta, "signature": {"format": "ssh", "fingerprint": "SHA256:AbC123"}}))
    await rig.tap(token(rig, mid, "▶"), message=mid)
    await rig.tap(token(rig, mid, "2/9 ▾"), message=mid)
    await rig.tap(token(rig, mid, "1"), message=mid)
    shown = text(rig, mid)
    assert "🔏 Commit signed (SSH, key SHA256:AbC123)" in shown and "not signed" not in shown


async def test_navigation_survives_cancel(rig: TgRig) -> None:
    card, mid = await _explore(rig)
    await rig.tap(token(rig, card.message_id, "✅ Approve"), message=card.message_id)
    await rig.tap(token(rig, card.message_id, "↩️ Cancel"), message=card.message_id)
    await rig.tap(token(rig, mid, "▶"), message=mid)
    assert "2/9" in text(rig, mid)
