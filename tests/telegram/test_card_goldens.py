"""AC 5 (F009-R1-5): golden S1 cards per kind in LIVE and DRY RUN, and the purposes and ``single_use``
flags each published card actually minted, against the §14.18.4.1 table. ``comment`` and
``issue_claim`` cards have no Explore, Files or Request changes; their 📝 Edit text opens the
``edit_comment`` ForceReply directly."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from ossbot.common.ai_policy import AiCondition, checklist_block
from ossbot.common.gitcmd import NumstatEntry
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import cards
from ossbot.gatekeeper.telegram.cards import AskedItem, CardView
from tests.conftest import insert_action, packaged_contribution
from tests.telegram import golden
from tests.telegram.reviewkit import labels, last_sent_id, latest_send, publish, token
from tests.telegram.test_interdiff import INTERDIFF_PATCH, _revised
from tests.telegram.tgharness import TgRig, make_rig

EXPIRES = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
D = ("approve", "reject", "reject_reason", "changes")
TABLE: dict[str, dict[str, int]] = {  # §14.18.4.1: purpose -> single_use
    "open_pr_r1": {**dict.fromkeys(D, 1), "explore": 0, "files": 0, "edit_text_pick": 0},
    "open_pr_rN": {**dict.fromkeys(D, 1), "explore": 0, "interdiff": 0, "edit_text_pick": 0},
    "push_update": {**dict.fromkeys(D, 1), "explore": 0, "files": 0},
    "comment": {"approve": 1, "reject": 1, "reject_reason": 1, "edit_text_pick": 0},
    "issue_claim": {"approve": 1, "reject": 1, "reject_reason": 1, "edit_text_pick": 0},
}


def view(kind: str, dry_run: bool, **kw: Any) -> CardView:
    base: dict[str, Any] = {
        "kind": kind, "cid": "c_0123456789", "upstream": "acme/widgets", "dry_run": dry_run,
        "title": "Fix the parser", "body": "Body\n\nAI assistance disclosure", "commit_message": "fix: parser",
        "numstat": (NumstatEntry(80, 20, "src/p.py"), NumstatEntry(25, 5, "tests/test_p.py")),
        "head_sha": "3" * 40, "round_base": "1" * 40, "base_ref": "main", "identity": "Test Person <t@example>",
        "branch": "ossbot/7-fix", "fork_owner": "bot", "rev": 1, "issue_number": 7, "issue_title": "Parser bug",
        "size_report": {"measured": {"code_added": 80, "code_deleted": 20, "test_added": 25, "test_deleted": 5,
                                     "files": 2}, "measured_class": "s", "approved_class": "s",
                        "estimate": {"code_lines": 100}, "tolerance": 0.5, "outside_areas": []},
        "expires_at": EXPIRES,
    }  # fmt: skip
    base.update(kw)
    return CardView(**base)


KINDS: dict[str, dict[str, Any]] = {
    "open_pr_r1": {"kind": "open_pr"},
    "open_pr_rN": {"kind": "open_pr", "rev": 2, "asked": (AskedItem("src/p.py L3", "y should be 4", touched=True),),
                   "asked_total": 1, "interdiff": {"mode": "interdiff", "files": [{"path": "src/p.py", "added": 1,
                                                                                    "deleted": 1}]}},
    "push_update": {"kind": "push_update", "pr_number": 12, "rev": 2},
    "comment": {"kind": "comment", "target_number": 12, "body": "Thanks for the review! Updated."},
    "issue_claim": {"kind": "issue_claim", "target_number": 7, "body": "Hi! I'd like to work on this issue."},
}  # fmt: skip


@pytest.mark.parametrize("mode", ["dry_run", "live"])
@pytest.mark.parametrize("name", list(KINDS))
def test_golden_s1_per_kind_and_mode(name: str, mode: str) -> None:
    out = cards.render(view(dry_run=mode == "dry_run", **KINDS[name]))
    assert not out.withheld and not out.too_large
    badge = "🧪 DRY RUN" if mode == "dry_run" else "🔴 LIVE"
    assert badge in out.card.split("\n", 1)[0]
    text = "\n\n----- part -----\n".join([out.card, *out.parts]) + "\n"
    golden.check(f"s1_{name}_{mode}.html", text)


def test_golden_s1_open_pr_with_ai_conditions() -> None:
    """§14.11.1 (post-v1): the AI-policy line with the checklist computed on the card's own title/body."""
    conds = [AiCondition(kind="disclosure"), AiCondition(kind="label", value="ai-assisted")]
    body = "Body\n\n" + checklist_block(conds[:1], login="bot", coder_model="a", reviewer_model="b")
    vetting = {"ai_policy": "allowed_with_conditions", "ai_conditions": [c.model_dump() for c in conds]}
    out = cards.render(view("open_pr", False, body=body + "\n\nAI assistance disclosure", vetting=vetting))
    assert "⚠️ AI policy: allowed with conditions (disclosure, label) · checklist ⚠️ missing: label" in out.card
    text = "\n\n----- part -----\n".join([out.card, *out.parts]) + "\n"
    golden.check("s1_open_pr_ai_conditions_live.html", text)
    full = "Body\n\n" + checklist_block(conds, login="bot", coder_model="a", reviewer_model="b")
    ok = cards.render(view("open_pr", False, body=full + "\n\nAI assistance disclosure", vetting=vetting))
    assert "✅ AI policy: allowed with conditions (disclosure, label) · checklist ✅ 2/2" in ok.card


# ----------------------------------------------------------------------------- published cards
@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def minted(ctx: AppContext, message_id: int) -> dict[str, int]:
    rows = ctx.conn.execute("SELECT purpose, single_use FROM ui_tokens WHERE tg_message_id = ? AND used_at IS NULL",
                            (message_id,)).fetchall()  # fmt: skip
    return {str(r["purpose"]): int(r["single_use"]) for r in rows}


async def _published(rig: TgRig, name: str) -> int:
    ctx = rig.ctx
    if name == "open_pr_rN":
        info = {"mode": "interdiff", "files": [{"path": "src/app.py", "added": 1, "deleted": 1,
                                                "new_ranges": [[3, 3]]}]}  # fmt: skip
        _, action = _revised(ctx, info, INTERDIFF_PATCH)
    elif name == "push_update":
        _, action = packaged_contribution(ctx, pr_number=7)
    elif name in {"comment", "issue_claim"}:
        cid, _ = packaged_contribution(ctx)
        action = insert_action(ctx, cid, name, {"target_number": 7, "body": "Hello there"})  # type: ignore[arg-type]
    else:
        _, action = packaged_contribution(ctx)
    return (await publish(rig, action)).message_id


@pytest.mark.parametrize("name", list(TABLE))
async def test_published_card_purposes_match_the_table(rig: TgRig, name: str) -> None:
    mid = await _published(rig, name)
    assert minted(rig.ctx, mid) == TABLE[name]
    shown = labels(rig, mid)
    if name in {"comment", "issue_claim"}:
        assert not any(label.startswith(("🧭 Explore", "🧾 Files", "✏️ Request changes")) for label in shown)
        await rig.tap(token(rig, mid, "📝 Edit text"), message=mid)
        prompt = last_sent_id(rig)
        sent = latest_send(rig, prompt)
        assert sent["reply_markup"]["force_reply"] is True
        pending = db.one(rig.ctx.conn, "SELECT kind FROM pending_input WHERE prompt_message_id = ?", (prompt,))
        assert pending is not None and pending["kind"] == "edit_comment"
    expires = db.one(rig.ctx.conn, "SELECT a.expires_at FROM approvals a WHERE a.tg_message_id = ?", (mid,))
    views = rig.ctx.conn.execute("SELECT expires_at FROM ui_tokens WHERE tg_message_id = ? AND single_use = 0",
                                 (mid,)).fetchall()  # fmt: skip
    assert expires is not None and all(v[0] == expires[0] for v in views)  # view tokens expire with the approval
    assert json.dumps(TABLE[name])  # the table is plain data
