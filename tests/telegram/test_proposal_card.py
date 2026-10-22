"""Gate 1: the proposal card (DESIGN §14.11.15, mockup 10; AC 38, AC 40, AC 43, AC 51): fields with 🤖
and ✅ labels, the mode badge "n/a: local change", the 4-row keyboard, two-tap Start work bound to the
version and hash, the budget rule, Details (no decision buttons) and Ask maintainers first."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest

from ossbot.common.clock import FakeClock, to_iso
from ossbot.common.schemas import DiscoveredFacts
from ossbot.gatekeeper import action_requests, db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import proposal_actions, proposal_cards
from ossbot.gatekeeper.telegram.ui import classic
from ossbot.runner.stages import fetch
from tests.telegram import golden
from tests.telegram.proposalkit import PCard, proposal_card, revise, state
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
    assert first.startswith("💡 <b>Proposal</b> · <code>" + card.cid + "</code> · v1")
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


CI_YES = {"ci_on_pr": "yes", "ci_kinds": ["tests", "lint"], "ci_checks": ["unit-tests-zz", "<b>@x</b> /kill"],
          "ci_sampled": 10, "ci_with_checks": 8, "ci_gating": "auto", "ci_source": "merged_prs"}  # fmt: skip


@pytest.mark.parametrize(
    ("ci", "line"),
    [
        ({}, "✅ <b>AI policy</b>: not stated · <b>CI on PRs</b>: ❔ unknown"),  # legacy vetting: no CI keys
        (CI_YES, "✅ <b>AI policy</b>: not stated · <b>CI on PRs</b>: ✅ tests, lint (8/10 merged PRs)"),
        ({**CI_YES, "ci_gating": "approval_required"},
         "<b>CI on PRs</b>: ✅ tests, lint (8/10 merged PRs) · runs after maintainer approval"),
        ({"ci_on_pr": "no", "ci_source": "merged_prs"}, "<b>CI on PRs</b>: ❌ none found"),
    ],
)  # fmt: skip
async def test_ci_on_prs_on_the_policy_line(rig: TgRig, ci: dict[str, object], line: str) -> None:
    """Post-v1 (§9.4, §14.11.15): CI facts on the same line, closed sets and counts only."""
    card = await proposal_card(rig, vetting={"claim_status": "unclaimed", "ai_policy": "unstated", **ci})
    body = text(rig, card.message_id)
    assert line in body and "unit-tests-zz" not in body  # check names only in Details


async def test_ci_check_names_in_details_are_escaped(rig: TgRig) -> None:
    card = await proposal_card(rig, vetting={"ai_policy": "unstated", **CI_YES})
    out = classic.render(proposal_cards.details_view(rig.ctx, card.cid, 1, 3), surface="card").text
    assert "✅ <b>CI on PRs</b>: ✅ tests, lint (8/10 merged PRs)" in out
    assert (
        "Checks seen on merged PRs: <code>unit-tests-zz</code> · <code>&lt;b&gt;@x&lt;/\u2060b&gt; /\u2060kill</code>"
        in out
    )
    assert "<b>@x</b>" not in out


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
    assert f"<code>{card.cid}</code> v1" in text(rig, mid) and "1/6 Problem &amp; approach" in text(rig, mid)
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


# --------------------------------------------------------------------------- post-v1 open discovery (§9)
OPEN = "openfaas/faas"
SEED_LABELS = ["✅ Start work", "⏭ Skip", "✏️ Adjust plan", "📏 Make it smaller", "🚫 Never…", "⏰ Later…",
               "🧭 Details"]  # fmt: skip
KEEP_ROW = ["⭐ Keep as target", "🚫 Never this repo"]


def discovered(ctx: AppContext, *, source: str = "search", topic: str | None = "gitops", stars: int | None = 24440,
               commit_days: int | None = None) -> None:  # fmt: skip
    """A ``candidate`` row for OPEN as the runner records it."""
    facts: dict[str, object] = {"repo": OPEN, "source": source, "status": "candidate", "topic": topic, "stars": stars}
    if commit_days is not None:
        facts["default_branch_commit_at"] = to_iso(ctx.clock.now() - timedelta(days=commit_days))
    ctx.discovered.record(DiscoveredFacts.model_validate(facts), "uid:2001/worker")


async def open_card(rig: TgRig, **row: object) -> PCard:
    discovered(rig.ctx, **row)  # type: ignore[arg-type]
    return await proposal_card(rig, contrib={"upstream": OPEN, "category": "open-discovery",
                                             "issue_url": f"https://github.com/{OPEN}/issues/7"})  # fmt: skip


def discovered_status(ctx: AppContext) -> str:
    found = ctx.discovered.get(OPEN)
    assert found is not None
    return found.status


def _header(body: str) -> list[str]:
    """The lines after the repo line, up to the issue line."""
    lines = body.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.startswith("<b>") and "/" in ln)
    return lines[start + 1 : next(i for i, ln in enumerate(lines) if ln.startswith("Issue "))]


async def test_open_discovery_card_line_and_keep_row(rig: TgRig) -> None:
    card = await open_card(rig, commit_days=3)
    body = text(rig, card.message_id)
    assert _header(body) == ["🔎 open discovery · topic gitops · ★ 24,440",
                             "🌿 last commit on the default branch: 3 d ago"]  # fmt: skip
    assert labels(rig, card.message_id) == SEED_LABELS + KEEP_ROW


@pytest.mark.parametrize(
    ("row", "line"),
    [
        ({"topic": None}, "🔎 open discovery · ★ 24,440"),
        ({"stars": None}, "🔎 open discovery · topic gitops"),
        ({"source": "hermes", "topic": None}, "🔎 suggested by Hermes · ★ 24,440"),
        ({"source": "hermes", "stars": None}, "🔎 suggested by Hermes"),
    ],
)  # fmt: skip
async def test_unknown_parts_are_left_out(rig: TgRig, row: dict[str, object], line: str) -> None:
    card = await open_card(rig, **row)
    assert _header(text(rig, card.message_id)) == [line]  # no commit date: no activity line
    assert labels(rig, card.message_id)[-2:] == KEEP_ROW


async def test_kept_repo_card_has_no_keep_row(rig: TgRig) -> None:
    discovered(rig.ctx)
    rig.ctx.discovered.decide(OPEN, "keep", "tg:4242")
    card = await proposal_card(rig, contrib={"upstream": OPEN, "category": "open-discovery"})
    assert _header(text(rig, card.message_id)) == ["⭐ kept target (open discovery) · ★ 24,440"]
    assert labels(rig, card.message_id) == SEED_LABELS


def _normalized(body: str, cid: str) -> str:
    """The card without the per-test values (cid, random issue number, expiry time)."""
    body = re.sub(r"<tg-time[^>]*>[^<]*</tg-time>", "<tg-time/>", body.replace(cid, "CID"))
    return re.sub(r">#\d+</a>", ">#N</a>", body)


async def test_seed_card_is_unchanged(rig: TgRig) -> None:
    """Config targets show no open-discovery line and no fifth row; the golden pins the bytes."""
    card = await proposal_card(rig, vetting={"claim_status": "unclaimed", "ai_policy": "unstated"})
    body = latest(rig, card.message_id)["text"]
    assert "open discovery" not in body and "🌿" not in body and "suggested by Hermes" not in body
    assert labels(rig, card.message_id) == SEED_LABELS
    golden.check("proposal_seed_card.html", _normalized(str(body), card.cid) + "\n")


async def test_seed_card_shows_the_default_branch_activity_from_vet(rig: TgRig) -> None:
    """Owner addition (2026-10-21): seed targets get the line too, from vet's read of the default branch."""
    when = (rig.ctx.clock.now() - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    card = await proposal_card(rig, vetting={"claim_status": "unclaimed", "ai_policy": "unstated",
                                             "default_branch_commit_at": when})  # fmt: skip
    assert _header(text(rig, card.message_id)) == ["🌿 last commit on the default branch: 8 d ago"]
    assert labels(rig, card.message_id) == SEED_LABELS


async def test_keep_is_two_taps_and_changes_only_the_db(rig: TgRig) -> None:
    card = await open_card(rig)
    tracked = {p.name: p.read_bytes() for p in rig.ctx.paths.config_dir.glob("*.yaml")}
    await rig.tap(token(rig, card.message_id, "⭐ Keep as target"), message=card.message_id)
    panel = text(rig, card.message_id)
    assert (
        "<b>Keep openfaas/faas as a target? It will be scanned every cycle like a seed target. Stored in the"
        " gatekeeper DB, not targets.yaml; undo with ossctl targets unblock --unkeep openfaas/faas.</b>"
    ) in panel
    assert labels(rig, card.message_id) == ["⭐ Keep this repo", "↩️ Cancel"]
    assert rig.buttons(latest(rig, card.message_id))[0][0]["style"] == "primary"
    assert discovered_status(rig.ctx) == "candidate"  # not yet
    await rig.tap(token(rig, card.message_id, "⭐ Keep this repo"), message=card.message_id)
    assert toast(rig) == "⭐ Kept: scanned every cycle."
    found = rig.ctx.discovered.get(OPEN)
    assert found is not None and found.status == "kept" and found.decided_by == "tg:4242"
    assert state(rig.ctx, card.cid) == "PROPOSED"  # Keep doesn't decide the proposal
    assert labels(rig, card.message_id) == SEED_LABELS  # the restored keyboard, without the fifth row
    assert "⭐ kept target (open discovery) · ★ 24,440" in text(rig, card.message_id)
    assert {p.name: p.read_bytes() for p in rig.ctx.paths.config_dir.glob("*.yaml")} == tracked


async def test_keep_cancel_restores_the_card(rig: TgRig) -> None:
    card = await open_card(rig)
    await rig.tap(token(rig, card.message_id, "⭐ Keep as target"), message=card.message_id)
    await rig.tap(token(rig, card.message_id, "↩️ Cancel"), message=card.message_id)
    assert labels(rig, card.message_id) == SEED_LABELS + KEEP_ROW
    assert discovered_status(rig.ctx) == "candidate"


async def test_keep_confirm_fails_closed(rig: TgRig, clock: FakeClock) -> None:
    card = await open_card(rig)
    await rig.tap(token(rig, card.message_id, "⭐ Keep as target"), message=card.message_id)
    confirm = token(rig, card.message_id, "⭐ Keep this repo")
    await rig.tap(confirm, message=card.message_id, user=999, chat=999)  # a stranger
    await rig.tap(confirm, message=card.message_id + 1)  # another message (forwarded)
    assert discovered_status(rig.ctx) == "candidate"
    clock.advance(seconds=rig.ctx.config.policy.limits.confirm_ttl_seconds + 1)
    await rig.tap(confirm, message=card.message_id)  # expired
    assert discovered_status(rig.ctx) == "candidate"


async def test_keep_on_a_superseded_version_is_refused(rig: TgRig) -> None:
    card = await open_card(rig)
    keep = token(rig, card.message_id, "⭐ Keep as target")
    await revise(rig, card.cid, 1, problem="a different problem")
    await rig.tap(keep, message=card.message_id)
    assert toast(rig) in (proposal_cards.SUPERSEDED, "This button is no longer valid.")
    assert discovered_status(rig.ctx) == "candidate"


async def test_keep_refused_once_the_repo_is_no_longer_a_candidate(rig: TgRig) -> None:
    card = await open_card(rig)
    await rig.tap(token(rig, card.message_id, "⭐ Keep as target"), message=card.message_id)
    rig.ctx.discovered.decide(OPEN, "never", "uid:1000/operator")  # ossctl targets block meanwhile
    await rig.tap(token(rig, card.message_id, "⭐ Keep this repo"), message=card.message_id)
    assert toast(rig) == proposal_actions.NOT_KEEPABLE
    assert discovered_status(rig.ctx) == "never"


async def test_never_on_a_discovered_repo_names_ossctl(rig: TgRig) -> None:
    card = await open_card(rig)
    await rig.tap(token(rig, card.message_id, "🚫 Never this repo"), message=card.message_id)
    assert (
        "Never propose work from openfaas/faas again? It won't be searched again. Undo with ossctl targets"
        " unblock openfaas/faas (there is no Telegram undo for discovered repos)."
    ) in text(rig, card.message_id)
    assert discovered_status(rig.ctx) == "candidate"  # not yet
    await rig.tap(token(rig, card.message_id, "🚫 Never this repo"), message=card.message_id)
    assert discovered_status(rig.ctx) == "never"
    assert state(rig.ctx, card.cid) == "PROPOSAL_REJECTED"
    assert rig.ctx.discovery_prefs.rows() == []  # not the config-target path


def test_claim_template_matches_the_runner() -> None:
    assert action_requests.CLAIM_TEMPLATE == fetch.CLAIM_BODY


def test_kind_labels_fit() -> None:
    for name in proposal_cards.KIND_NAMES.values():
        assert len(name) <= 16
        assert len(f"🚫 Kind: {name}") <= 24
