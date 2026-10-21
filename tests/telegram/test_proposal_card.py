"""Gate 1: the proposal card (DESIGN §14.11.15, mockup 10; AC 38, AC 40, AC 43, AC 51): fields with 🤖
and ✅ labels, the mode badge "n/a: local change", the 4-row keyboard, two-tap Start work bound to the
version and hash, the budget rule, Details (no decision buttons) and Ask maintainers first."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from ossbot.gatekeeper import action_requests, db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import proposal_cards
from ossbot.runner.stages import fetch
from tests.telegram.proposalkit import proposal_card, state
from tests.telegram.reviewkit import labels, latest, text, toast, token, view_message
from tests.telegram.tgharness import TgRig, make_rig

ASSIGN = {"requires_assignment": True, "claim_status": "unclaimed", "ai_policy": "allowed"}


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


async def test_card_fields_badge_and_keyboard(rig: TgRig) -> None:
    card = await proposal_card(rig, vetting={"claim_status": "unclaimed"}, rationale="maintainer asked for a PR")
    body = text(rig, card.message_id)
    first = body.split("\n", 1)[0]
    assert first.startswith("💡 <b>Proposal</b> · " + card.cid + " · v1")
    assert "🧪 DRY RUN (n/a: local change)" in first
    assert "<b>langfuse/langfuse</b>" in body
    assert "<b>Fix trace export when spans are empty</b> · Bug" in body
    assert "🤖 <b>Problem</b>" in body and "🤖 <b>Approach</b>" in body
    assert "  1. Guard empty span lists in toOtel()" in body
    assert "🤖 <b>Likely files</b> <code>web/src/export/</code>" in body
    assert "Size 🤖 est <b>s</b> · ~30 code · ~40 test · 2 files · ✅ fits your auto size ≤" in body
    assert "Validate ✅ tiers ≤ T1 · 🤖 " in body  # repo facts ✅, the scout's plan 🤖 (§14.11.15)
    assert "✅ Signals: no competing claim · assignment not required" in body
    assert "✅ <b>AI policy</b>: not recorded" in body  # a vetting without a status (legacy row)
    assert "🤖 <b>Why it fits</b>" in body
    assert "Confidence 🤖 0.78 · Risks 🤖 0 · Alternatives 🤖 0 → 🧭 Details" in body
    assert "⏳ Proposal expires <tg-time" in body
    assert len(body) <= 3900
    assert labels(rig, card.message_id) == ["✅ Start work", "⏭ Skip", "✏️ Adjust plan", "📏 Make it smaller",
                                            "🚫 Never…", "⏰ Later…", "🧭 Details"]  # fmt: skip
    start = rig.buttons(latest(rig, card.message_id))[0][0]
    assert start["style"] == "success"
    # silent: an autonomous proposal never rings (class 4)
    sent = db.one(rig.ctx.conn, "SELECT payload_json FROM tg_outbox WHERE dedupe_key = ?", (f"card:{card.approval}",))
    assert '"disable_notification": true' in str(sent[0])  # type: ignore[index]


async def test_user_directed_card_rings_and_skips_the_size_preference(rig: TgRig) -> None:
    card = await proposal_card(rig, source="user", notes="keep it minimal", vetting=ASSIGN)
    body = text(rig, card.message_id)
    assert '👤 <b>requested by you</b> · "keep it minimal"' in body
    assert "👤 size preference not applied" in body and "fits your auto size" not in body
    assert labels(rig, card.message_id)[-2:] == ["🧭 Details", "🙋 Ask maintainers first"]
    sent = db.one(rig.ctx.conn, "SELECT payload_json FROM tg_outbox WHERE dedupe_key = ?", (f"card:{card.approval}",))
    assert '"disable_notification": false' in str(sent[0])  # type: ignore[index]


@pytest.mark.parametrize(
    ("vetting", "line"),
    [
        ({"ai_policy": "unstated"}, "✅ <b>AI policy</b>: not stated"),
        ({"ai_policy": "unclear"}, "✅ <b>AI policy</b>: unclear (allowed by your rule)"),
        ({"ai_policy": "allowed"}, "✅ <b>AI policy</b>: allowed"),
        ({"ai_policy": "disclosure_required"}, "✅ <b>AI policy</b>: allowed with conditions (disclosure)"),
        ({"ai_policy": "allowed_with_conditions", "ai_conditions": [{"kind": "disclosure"},
                                                                    {"kind": "label", "value": "ai"}]},
         "✅ <b>AI policy</b>: allowed with conditions (disclosure, label)"),
    ],
)  # fmt: skip
async def test_ai_policy_fact_line(rig: TgRig, vetting: dict[str, object], line: str) -> None:
    """Post-v1 (§9.5, §14.11.15): the AI-policy line replaces the old AI signal."""
    card = await proposal_card(rig, vetting={"claim_status": "unclaimed", **vetting})
    body = text(rig, card.message_id)
    assert line in body and "AI contributions" not in body


async def test_untrusted_fields_are_escaped_and_defused(rig: TgRig) -> None:
    card = await proposal_card(rig, problem="<b>boom</b> run /dryrun off now", approach=["<i>x</i> /kill", "b"])
    body = latest(rig, card.message_id)["text"]
    assert "&lt;b&gt;boom&lt;/b&gt;" in text(rig, card.message_id) and "<b>boom</b>" not in body
    assert "/\u2060dryrun" in body and "/\u2060kill" in body


async def test_budget_rule_keeps_the_required_lines(rig: TgRig) -> None:
    card = await proposal_card(
        rig, problem="p" * 300, approach=[f"step {i} " + "x" * 90 for i in range(6)],
        areas=[f"src/{'d' * 50}/{i}.py" for i in range(30)], rationale="r" * 160,
        tests=["t" * 200] * 20, risks=["k" * 200] * 10, validation_plan="v" * 400,
        vetting={"claim_status": "unclaimed", "ai_policy": "allowed"},
    )  # fmt: skip
    body = text(rig, card.message_id)
    assert len(latest(rig, card.message_id)["text"]) <= 3900
    for needed in ("💡 <b>Proposal</b>", "DRY RUN", "Issue ", "Size 🤖 est", "⏳ Proposal expires"):
        assert needed in body
    if "AI policy" not in body:  # the AI-policy line has the highest optional priority: Signals go first
        assert "✅ Signals" not in body


async def test_forbidden_paths_disable_start_work(rig: TgRig) -> None:
    card = await proposal_card(rig, areas=[".github/workflows/ci.yml"])
    body = text(rig, card.message_id)
    assert "⛔ Touches forbidden paths 🤖 <code>.github/workflows/ci.yml</code>" in body
    start = rig.buttons(latest(rig, card.message_id))[0][0]
    assert start == {"text": "⛔ Not startable", "disabled": {}}


async def test_forbidden_path_warning_defuses_scout_text(rig: TgRig) -> None:
    """U6/U8: '*' in a forbidden glob matches spaces, so scout text reaches this line; it is made
    visible and defused like every other scout field (F009-R2-5)."""
    card = await proposal_card(rig, areas=[".github/workflows/x /kill \u202eevil"])
    sent = latest(rig, card.message_id)["text"]
    line = next(ln for ln in sent.split("\n") if ln.startswith("⛔ Touches forbidden paths"))
    assert "/\u2060kill" in line and "/kill" not in line
    assert "\u202e" not in line and "⟦U+202E⟧" in line


async def test_start_work_is_two_taps_and_binds_the_version(rig: TgRig) -> None:
    card = await proposal_card(rig, vetting={"cla_required": True})
    await rig.tap(token(rig, card.message_id, "✅ Start work"), message=card.message_id)
    panel = text(rig, card.message_id)
    sha = str(db.one(rig.ctx.conn, "SELECT proposal_sha256 FROM proposals WHERE cid = ?", (card.cid,))[0])  # type: ignore[index]
    assert "Confirm: start work on langfuse/langfuse#" in panel
    assert f"(proposal v1 · est s · sha256 {sha[:4]}…{sha[-4:]})" in panel
    assert "Local work only — dry-run does not apply; nothing is public before you approve the PR." in panel
    assert "⚠️ CLA required by this repo — confirming means you have signed it" in panel
    confirm = rig.buttons(latest(rig, card.message_id))[0][0]
    assert confirm["text"] == "🛠 Confirm (CLA signed)" and confirm["style"] == "primary"
    assert state(rig.ctx, card.cid) == "PROPOSED"
    await rig.tap(token(rig, card.message_id, "🛠 Confirm"), message=card.message_id)
    assert state(rig.ctx, card.cid) == "PROPOSAL_APPROVED"


async def test_start_work_refused_while_paused(rig: TgRig) -> None:
    card = await proposal_card(rig)
    rig.ctx.control.pause("test", "uid:1000/operator")
    await rig.tap(token(rig, card.message_id, "✅ Start work"), message=card.message_id)
    assert "Paused or killed" in toast(rig)
    assert state(rig.ctx, card.cid) == "PROPOSED"


async def test_details_has_sections_and_no_decision_buttons(rig: TgRig) -> None:
    card = await proposal_card(rig, risks=["could break exporters"], alternatives=[{"summary": "do less"}])
    await rig.tap(token(rig, card.message_id, "🧭 Details"), message=card.message_id)
    mid = view_message(rig.ctx, "proposal_details")
    assert f"{card.cid} v1" in text(rig, mid) and "1/6 Problem &amp; approach" in text(rig, mid)
    purposes = {r[0] for r in rig.ctx.conn.execute("SELECT purpose FROM ui_tokens WHERE tg_message_id = ?", (mid,))}
    assert purposes == {"proposal_details_nav"}
    await rig.tap(token(rig, mid, "▶"), message=mid)
    assert "2/6 Files &amp; tests" in text(rig, mid)
    await rig.tap(token(rig, mid, "▶"), message=mid)
    await rig.tap(token(rig, mid, "▶"), message=mid)
    assert "could break exporters" in text(rig, mid) and "do less" in text(rig, mid)


async def test_ask_maintainers_first_creates_a_claim_card(rig: TgRig) -> None:
    card = await proposal_card(rig, vetting=ASSIGN)
    await rig.tap(token(rig, card.message_id, "🙋 Ask maintainers first"), message=card.message_id)
    assert "claim card follows" in toast(rig)
    claim = db.one(rig.ctx.conn, "SELECT id, payload_json FROM outbound_actions WHERE cid = ? AND kind ="
                   " 'issue_claim'", (card.cid,))  # fmt: skip
    assert claim is not None
    assert db.one(rig.ctx.conn, "SELECT 1 FROM approvals WHERE action_id = ?", (claim["id"],)) is not None
    stored = json.loads(claim["payload_json"])["body"]
    login = rig.ctx.config.profile.github_login
    # the bytes of the actions path: the AI-assistance line and the reconciliation marker (§12)
    assert stored == action_requests.claim_body(login) + f"\n\n<!-- ossbot:action={claim['id']} -->"
    assert f"— drafted with AI assistance, reviewed by @{login}" in stored
    assert state(rig.ctx, card.cid) == "PROPOSED"  # the proposal stays open
    await rig.tap(token(rig, card.message_id, "🙋 Ask maintainers first"), message=card.message_id)
    assert toast(rig) == "A claim card is already waiting."
    # the runner's fetch stage asks again after Start work: it gets this claim back, not a second one
    issue = db.one(rig.ctx.conn, "SELECT issue_number FROM contributions WHERE id = ?", (card.cid,))
    assert issue is not None
    again = action_requests.request_action(rig.ctx, card.cid, "issue_claim", int(issue[0]), "x", "uid:1/worker")
    assert again.reused and again.action_id == claim["id"]
    n = db.one(rig.ctx.conn, "SELECT COUNT(*) FROM outbound_actions WHERE cid = ? AND kind = 'issue_claim'",
               (card.cid,))  # fmt: skip
    assert n is not None and n[0] == 1


async def test_ask_maintainers_first_refused_while_paused(rig: TgRig) -> None:
    card = await proposal_card(rig, vetting=ASSIGN)
    rig.ctx.control.pause("test", "uid:1000/operator")
    await rig.tap(token(rig, card.message_id, "🙋 Ask maintainers first"), message=card.message_id)
    assert toast(rig).startswith("Paused or killed: nothing was requested")
    assert db.one(rig.ctx.conn, "SELECT 1 FROM outbound_actions WHERE cid = ?", (card.cid,)) is None


def test_claim_template_matches_the_runner() -> None:
    assert action_requests.CLAIM_TEMPLATE == fetch.CLAIM_BODY


def test_kind_labels_fit() -> None:
    for name in proposal_cards.KIND_NAMES.values():
        assert len(name) <= 16
        assert len(f"🚫 Kind: {name}") <= 24
