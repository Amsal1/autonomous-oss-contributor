"""Rich read-only views behind ``auto`` with classic fallback (DESIGN §14.17.3, §14.20.5; AC 64, BA-9).

The explorer, file and interdiff views go out as ``sendRichMessage`` while ``rich_views`` is auto or on;
a message sent rich is always edited rich (never a classic ``editMessageText`` on it); a rich edit that
returns 400 sends the classic view as a new reply to the card, stubs the old message "moved ↓", and its
buttons answer "Superseded…"; ``rich_views: off`` and ``file_view: classic`` keep them classic."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.prefs import UxPrefs
from tests.telegram.reviewkit import Card, latest, latest_send, open_card, token, view_message
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


async def _rich_files(rig: TgRig, **prefs: str) -> tuple[Card, int]:
    card = await open_card(rig)  # sets rich_views off for the card's classic assertions
    p = UxPrefs(rig.ctx.conn, rig.ctx.clock)
    p.set("rich_views", "auto")
    for key, value in prefs.items():
        p.set(key, value)
    await rig.tap(token(rig, card.message_id, "🧾 Files"), message=card.message_id)
    return card, view_message(rig.ctx, "file")


def _method_of(rig: TgRig, message_id: int) -> str:
    row = db.one(rig.ctx.conn, "SELECT method FROM tg_outbox WHERE result_message_id = ? AND status = 'sent'"
                               " ORDER BY id LIMIT 1", (message_id,))  # fmt: skip
    return str(row["method"]) if row is not None else ""


async def test_file_view_is_rich_and_edits_stay_rich(rig: TgRig) -> None:
    _, mid = await _rich_files(rig)
    assert _method_of(rig, mid) == "sendRichMessage"
    await rig.tap(token(rig, mid, "Next file ▶"), message=mid)
    edit = rig.req.last("editMessageText")
    assert edit["message_id"] == mid and "rich_message" in edit and "text" not in edit
    assert "tests/test_app.py" in json.dumps(edit["rich_message"])


async def test_a_refused_rich_edit_moves_the_classic_view_below(rig: TgRig) -> None:
    card, mid = await _rich_files(rig)
    rig.req.fail("editMessageText", "Bad Request: rich messages can't be edited here")
    await rig.tap(token(rig, mid, "Next file ▶"), message=mid)
    await rig.flush()
    resent = [p for m, p in rig.req.calls if m == "sendMessage"
              and "tests/test_app.py" in str(p.get("text", "")).replace("\u2060", "")]  # fmt: skip
    assert resent, [(m, str(p)[:300]) for m, p in rig.req.calls[-6:]]
    assert resent and resent[-1]["reply_parameters"]["message_id"] == card.message_id  # a reply to the card
    stub = [p for m, p in rig.req.calls if m == "editMessageText" and p.get("message_id") == mid][-1]
    assert "moved ↓" in json.dumps(stub, ensure_ascii=False)
    assert db.one(rig.ctx.conn, "SELECT stale FROM tg_views WHERE message_id = ?", (mid,))[0] == 1
    old = rig.token_of(latest_send(rig, mid), "Next file ▶")  # a button of the old (now stubbed) message
    await rig.tap(old, message=mid)
    assert "no longer valid" in str(rig.req.last("answerCallbackQuery").get("text", "")).lower() or \
        "superseded" in str(rig.req.last("answerCallbackQuery").get("text", "")).lower()  # fmt: skip


@pytest.mark.parametrize("prefs", [{"rich_views": "off"}, {"file_view": "classic"}])
async def test_off_and_classic_file_view_keep_it_classic(rig: TgRig, prefs: dict[str, str]) -> None:
    _, mid = await _rich_files(rig, **prefs)
    assert _method_of(rig, mid) == "sendMessage"
    assert '<pre><code class="language-diff">' in str(latest(rig, mid).get("text", ""))


async def test_explorer_is_rich_under_auto(rig: TgRig) -> None:
    card = await open_card(rig)
    UxPrefs(rig.ctx.conn, rig.ctx.clock).set("rich_views", "auto")
    await rig.tap(token(rig, card.message_id, "🧭 Explore"), message=card.message_id)
    assert _method_of(rig, view_message(rig.ctx, "explorer")) == "sendRichMessage"
