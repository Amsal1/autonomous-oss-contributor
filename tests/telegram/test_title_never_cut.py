"""The PR title is publish bytes (DESIGN §14.4, U14; PLAN item 45): a 256-character title is shown in full
on the S1 card, the S8 Confirm panel and explorer section 9, escaped but never cut, defused or redacted,
and the bound ``title_sha256`` is the hash of exactly those bytes."""

from __future__ import annotations

import html
import json
import re
from collections.abc import AsyncIterator

import pytest

from ossbot.common.hashing import sha256_text
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import cards
from tests.conftest import packaged_contribution
from tests.telegram import golden
from tests.telegram.reviewkit import publish, text, token, view_message
from tests.telegram.tgharness import TgRig, make_rig

# 256 characters with every escaping hazard and a command-looking word that must not be defused
TITLE = ('Fix <parser> & "quotes" in /start handling: ' + "x" * 200 + " — end")[:256]
TITLE = TITLE + "y" * (256 - len(TITLE))


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def _code(shown: str) -> str:
    match = re.search(r"<code>(.*?)</code>", shown.split("PR title: ", 1)[-1], re.S)
    assert match is not None
    return html.unescape(match.group(1))


def test_title_fixture_is_256_characters() -> None:
    assert len(TITLE) == 256 and "/start" in TITLE


async def test_the_full_title_on_card_confirm_and_explorer(rig: TgRig) -> None:
    ctx = rig.ctx
    cid, action = packaged_contribution(ctx)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE contributions SET pr_title = ? WHERE id = ?", (TITLE, cid))
    card = await publish(rig, action)
    assert _code(text(rig, card.message_id)) == TITLE
    binding = json.loads(db.one(ctx.conn, "SELECT binding_json FROM approvals WHERE id = ?",
                                (card.approval,))[0])  # fmt: skip
    assert binding["title_sha256"] == sha256_text(TITLE)
    await rig.tap(token(rig, card.message_id, "✅ Approve"), message=card.message_id)
    assert _code(text(rig, card.message_id)) == TITLE  # the S8 panel repeats the full title
    await rig.tap(token(rig, card.message_id, "↩️ Cancel"), message=card.message_id)
    await rig.tap(token(rig, card.message_id, "🧭 Explore"), message=card.message_id)
    explorer = view_message(ctx, "explorer")
    await rig.tap(token(rig, explorer, "1/9 ▾"), message=explorer)
    await rig.tap(token(rig, explorer, "9"), message=explorer)
    match = re.search(r"Title <code>(.*?)</code>", text(rig, explorer), re.S)
    assert match is not None and html.unescape(match.group(1)) == TITLE


def test_golden_card_with_a_256_character_title(ctx: AppContext) -> None:
    cid, action = packaged_contribution(ctx)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE contributions SET pr_title = ?, issue_number = 1000 WHERE id = ?", (TITLE, cid))
    out = cards.render(cards.view_for_action(ctx.conn, action, True, None))
    golden.check("s1_title_256.html", out.card.replace(cid, "c_CID") + "\n")
