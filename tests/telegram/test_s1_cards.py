"""S1 cards per kind (DESIGN §14.11.1, §14.4a; AC 4, AC 39, AC 54, AC 56): the size line (estimate vs
actual, test lines apart), the 👤 marker, "You asked", facts vs 🤖 claims, and the two bug stubs."""

from __future__ import annotations

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
from ossbot.gatekeeper.telegram.review.size_line import overrun_pct, size_lines
from ossbot.gatekeeper.telegram.ui import classic
from ossbot.gatekeeper.telegram.ui import kit as k
from tests.conftest import packaged_contribution
from tests.telegram.reviewkit import status
from tests.telegram.tgharness import TgRig, make_rig

GHP = "ghp_" + "Q8xk2LmZ9vR4tYp7" + "Wn3bJc6Hd1Fs5Ga0Ke8U"  # assembled at runtime
EXPIRES = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def report(code_lines: int = 100, *, added: int = 80, deleted: int = 20, measured: str = "s",
           approved: str = "s", outside: list[str] | None = None) -> dict[str, Any]:  # fmt: skip
    return {"measured": {"code_added": added, "code_deleted": deleted, "test_added": 25, "test_deleted": 5,
                         "files": 3}, "measured_class": measured, "approved_class": approved,
            "estimate": {"code_lines": code_lines}, "tolerance": 0.5, "outside_areas": outside or []}  # fmt: skip


def view(**kw: Any) -> CardView:
    base: dict[str, Any] = {
        "kind": "open_pr", "cid": "c_0123456789", "upstream": "acme/widgets", "dry_run": True,
        "title": "Fix the parser", "body": "Body\n\nAI assistance disclosure", "commit_message": "fix: parser",
        "numstat": (NumstatEntry(80, 20, "src/p.py"), NumstatEntry(25, 5, "tests/test_p.py")),
        "head_sha": "3" * 40, "round_base": "1" * 40, "base_ref": "main", "identity": "Test Person <t@example>",
        "branch": "ossbot/7-fix", "fork_owner": "bot", "rev": 1, "issue_number": 7, "issue_title": "Parser bug",
        "size_report": report(), "expires_at": EXPIRES,
    }  # fmt: skip
    base.update(kw)
    return CardView(**base)


def text_of(lines: tuple[k.Line, ...]) -> str:
    out = classic.render(k.View(kind="fyi", blocks=[k.Block((k.Lines(lines),))]), surface="test")
    return out.text


def test_size_line_estimate_vs_actual() -> None:
    facts = size_lines(report())
    assert not facts.flagged
    assert text_of(facts.lines) == "Size ✅ s · est ~100 code → actual +80 / −20 code · +30 test · 3 files"


def test_size_overrun_is_flagged_with_the_coder_claim() -> None:
    facts = size_lines(report(added=180, deleted=20, measured="m"), coder_reason="the parser had two copies")
    assert facts.flagged and facts.overrun_pct == 100
    shown = text_of(facts.lines)
    assert shown.startswith("Size ⚠️ m (est s) · est ~100 code → actual +180 / −20 code")
    assert '⚠️ <b>+100% over estimate</b> · 🤖 coder: "the parser had two copies"' in shown


def test_size_with_a_zero_estimate_has_no_percentage() -> None:
    facts = size_lines(report(code_lines=0, added=3, deleted=0))
    assert facts.flagged and facts.overrun_pct is None
    assert "⚠️ <b>est 0 code</b> · actual +3 / −0 code" in text_of(facts.lines)
    assert overrun_pct(10, 0) is None and overrun_pct(150, 100) == 50
    # AC 54's worked examples: +142 / −30 code against est 40 and est 48
    assert overrun_pct(142 + 30, 40) == 330 and overrun_pct(142 + 30, 48) == 258


def test_files_outside_the_plan() -> None:
    shown = text_of(size_lines(report(outside=["a.py", "b.py", "c.py", "d.py"])).lines)
    assert "⚠️ 4 files outside the plan: a.py, b.py, c.py +1" in shown


def test_open_pr_card_layout() -> None:
    out = cards.render(view())
    assert not out.withheld and not out.too_large
    lines = out.card.split("\n")
    assert lines[0].startswith("🧩 <b>Ready for review</b> · c_0123456789 · r1")
    assert "🧪 DRY RUN" in lines[0]
    assert "<b>acme/\u2060widgets</b>" in out.card  # a word joiner keeps owner/repo from auto-linking
    assert "Issue #7 <b>Parser bug</b>" in out.card
    assert "Size ✅ s · est ~100 code" in out.card
    assert "PR title: <code>Fix the parser</code>" in out.card
    assert '⏳ Approval expires <tg-time unix="1792497600"' in out.card
    assert out.part_kinds == ["body_part", "commit_msg"]
    assert out.parts[0].startswith("<b>PR body (1/1)</b>\n<pre>")


def test_ai_policy_line_on_the_open_pr_card() -> None:
    """Post-v1 (§14.11.1): fixed wording from the stored vetting; nothing without one (old rows)."""
    assert "AI policy" not in cards.render(view()).card
    silent = cards.render(view(vetting={"ai_policy": "unstated"})).card
    assert "✅ AI policy: not stated" in silent and "checklist" not in silent
    conds = [AiCondition(kind="human_review"), AiCondition(kind="title_tag", value="[AI]")]
    vetting = {"ai_policy": "allowed_with_conditions", "ai_conditions": [c.model_dump() for c in conds]}
    body = checklist_block(conds, login="bot", coder_model="a", reviewer_model="b")
    ok = cards.render(view(title="[AI] Fix", body=body, vetting=vetting)).card
    assert "allowed with conditions (human review, title tag) · checklist ✅ 2/2" in ok
    edited = cards.render(view(title="Fix", body=body, vetting=vetting)).card  # a human edit dropped the tag
    assert "checklist ⚠️ missing: title tag" in edited
    hostile = {"ai_policy": "<b>allowed</b>", "ai_conditions": [{"kind": "label", "value": "@x <b>"}]}
    assert "AI policy: not recorded" in cards.render(view(vetting=hostile)).card
    assert "AI policy" not in cards.render(view(kind="push_update", pr_number=1, vetting=vetting)).card


def test_user_directed_marker_and_notes() -> None:
    out = cards.render(view(user_directed=True, notes="keep it small"))
    assert '👤 <b>requested by you</b> · Notes: "keep it small"' in out.card


def test_model_claims_are_labelled() -> None:
    out = cards.render(view(risk="low", confidence=0.8, review={"verdict": "approve", "blocking": [], "nits": [1]},
                            models={"reviewer": "m1", "coder": "m2"}, rationale="Small, contained fix"))  # fmt: skip
    assert "Risk 🤖 low (0.80)" in out.card
    assert "Self-review 🤖 APPROVE (reviewer ≠ coder ✅) · 0 blocking · 1 nits" in out.card
    assert "🤖 <b>Why</b> Small, contained fix" in out.card


def test_push_update_and_comment_headers() -> None:
    push = cards.render(view(kind="push_update", pr_number=12, rev=2))
    assert push.card.startswith("🔁 <b>Fix ready for PR #12</b> · c_0123456789 · r2")
    assert "incremental commit on" in push.card and push.part_kinds == ["commit_msg"]
    comment = cards.render(view(kind="comment", target_number=12, body="Thanks!"))
    assert "Target: <b>acme/widgets#12</b>" in comment.card.replace("\u2060", "")
    assert comment.part_kinds == ["body_part"]


def test_you_asked_lists_five_then_more() -> None:
    asked = tuple(AskedItem(f"p.py L{i}", f"comment {i}", touched=i % 2 == 0) for i in range(1, 8))
    out = cards.render(view(rev=2, asked=asked, asked_total=7,
                            interdiff={"mode": "interdiff", "files": [{"path": "src/p.py", "added": 3,
                                                                       "deleted": 1}]}))  # fmt: skip
    assert out.card.startswith("🔁 <b>Revised for review</b>")
    assert "<b>You asked</b> (7 comments):" in out.card
    assert "1. p.py L1 — comment 1 · ⚠️ not touched" in out.card
    assert "2. p.py L2 — comment 2 · ✅ touched" in out.card
    assert "6. " not in out.card and "+2 more → 🧭 Explore · 9 PR text" in out.card
    assert "<b>What changed since r1</b> ✅ (base unchanged): src/p.py +3 −1" in out.card.replace("\u2060", "")


def test_hidden_characters_are_shown_and_counted() -> None:
    out = cards.render(view(title="Fix\u202eparser"))
    assert "⟦U+202E⟧" in out.card and "1 invisible/bidi/control character(s)" in out.card


def test_a_secret_in_the_publish_bytes_withholds_the_card() -> None:
    out = cards.render(view(body=f"token {GHP}"))
    assert out.withheld and out.parts == []
    assert GHP not in out.card and "Card withheld" in out.card


def test_an_unrenderable_card_is_a_fixed_stub() -> None:
    out = cards.render(view(title="x" * 5000))
    assert out.too_large and out.parts == [] and "Card too large to render (bug)" in out.card


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


async def test_a_withheld_card_invalidates_its_approval(rig: TgRig) -> None:
    ctx = rig.ctx
    cid, action = packaged_contribution(ctx)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE contributions SET pr_body = ? WHERE id = ?", (f"x {GHP}", cid))
        approval = ctx.create_approval(action, "test")
    ctx.on_approval_created(approval)
    await rig.flush()
    assert status(ctx, approval) == "invalidated"
    assert db.one(ctx.conn, "SELECT invalidated_reason FROM approvals WHERE id = ?", (approval,))[0] == "render_failed"
    (row,) = ctx.conn.execute("SELECT approval_id, payload_json FROM tg_outbox WHERE dedupe_key = ?",
                              (f"card:{approval}",)).fetchall()  # fmt: skip
    assert row["approval_id"] is None and GHP not in row["payload_json"]
    assert "reply_markup" not in rig.sent()[-1]
