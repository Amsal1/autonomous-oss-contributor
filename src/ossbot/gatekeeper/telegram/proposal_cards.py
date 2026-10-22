"""Gate 1: the proposal card, its version diff, Details and the Start-work Confirm panel (DESIGN
§14.11.15-§14.11.16; mockup 10; PLAN item 46).

A classic message in ✅ Reviews (U3: never rich, Start work only on the card). Every scout field is
labelled 🤖 and quoted (U6), every gatekeeper fact carries ✅, untrusted text is made visible and
defused (U8). The kit's budget rule drops Signals, then the rationale, then the Approach bullets after
3, before it would cut the header, the mode line, the issue line, the size line or the expiry.

Keyboard (4 rows of ≤ 2): ``✅ Start work`` is the decision token of the ``p_`` approval; the rest are
kit tokens (``approval_id`` NULL, ``arg = {cid, version}``) bound to the card message, resolved at tap
time by ``(cid, version)`` (``telegram/proposal_actions.py``).
"""

from __future__ import annotations

import fnmatch
import json
import sqlite3
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from ossbot.common import ai_policy, ci_facts
from ossbot.common.clock import from_iso
from ossbot.common.config import resolve_target
from ossbot.common.schemas import DiscoveredView
from ossbot.common.size import SIZE_CLASSES
from ossbot.gatekeeper import db
from ossbot.gatekeeper.telegram import keyboards
from ossbot.gatekeeper.telegram.cards import CardMessages
from ossbot.gatekeeper.telegram.escape import esc
from ossbot.gatekeeper.telegram.ui import classic, icons, ids
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui.limits import middle_cut

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

# Fixed display names (≤ 16 characters): never repo or model text (§14.14.4).
KIND_NAMES: dict[str, str] = {
    "bug": "Bug", "docs": "Docs", "tests": "Tests", "small-feature": "Small feature", "ci-fix": "CI fix",
    "refactor-small": "Small refactor", "breaking-change": "Breaking change", "new-dependency": "New dependency",
    "api-redesign": "API redesign", "security-sensitive-crypto": "Crypto/security",
}  # fmt: skip
APPROACH_MAX, APPROACH_REQUIRED = 6, 3
FILES_MAX = 4
DIFF_LINES, DIFF_LINE_MAX = 6, 100
KIT_TTL = timedelta(hours=72)
SUPERSEDED = "Superseded: this proposal changed; review the new version."
EXPIRED = "This proposal expired; a fresh one will arrive if the issue is still open."
PROPOSAL_PURPOSES = ("proposal_skip", "proposal_adjust", "proposal_smaller", "proposal_never", "proposal_later",
                     "proposal_details", "proposal_ask_first")  # fmt: skip


def kind_name(kind: str | None) -> str:
    return KIND_NAMES.get(kind or "", "this kind")


# --------------------------------------------------------------------------- data
def load(conn: sqlite3.Connection, cid: str, version: int) -> dict[str, Any] | None:
    row = db.one(conn, "SELECT proposal_json, proposal_sha256, est_class, feedback, created_at FROM proposals"
                 " WHERE cid = ? AND version = ?", (cid, version))  # fmt: skip
    if row is None:
        return None
    doc: dict[str, Any] = json.loads(row["proposal_json"])
    return {**doc, "_sha256": row["proposal_sha256"], "_class": row["est_class"], "_feedback": row["feedback"],
            "_created_at": row["created_at"]}  # fmt: skip


def _json(value: Any) -> dict[str, Any]:
    try:
        doc = json.loads(value) if value else {}
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def version_diff(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Deterministic field-by-field diff of two stored proposals (§14.11.16); every line quotes 🤖 text."""
    out: list[str] = []
    if old.get("problem") != new.get("problem"):
        out.append(f"~ Problem: {new.get('problem', '')}")
    a, b_ = list(old.get("approach") or []), list(new.get("approach") or [])
    for i in range(max(len(a), len(b_))):
        before, after = (a[i] if i < len(a) else None), (b_[i] if i < len(b_) else None)
        if before != after:
            if after is not None:
                out.append(f"+ Approach {i + 1}: {after}")
            if before is not None:
                out.append(f"− Approach {i + 1}: {before}")
    files_a, files_b = set(old.get("areas") or []), set(new.get("areas") or [])
    out += [f"+ File: {f}" for f in sorted(files_b - files_a)] + [f"− File: {f}" for f in sorted(files_a - files_b)]
    ea, eb = old.get("estimate") or {}, new.get("estimate") or {}
    if (old.get("_class"), ea.get("code_lines"), ea.get("test_lines")) != (
            new.get("_class"), eb.get("code_lines"), eb.get("test_lines")):  # fmt: skip
        code = f"~{ea.get('code_lines')} → ~{eb.get('code_lines')} code"
        test = f"~{ea.get('test_lines')} → ~{eb.get('test_lines')} test"
        out.append(f"Size: {old.get('_class')} → {new.get('_class')} · {code} · {test}")
    if old.get("validation_plan") != new.get("validation_plan"):
        out.append(f"~ Validation: {new.get('validation_plan', '')}")
    risks_a, risks_b = set(old.get("risks") or []), set(new.get("risks") or [])
    out += [f"+ Risk: {r}" for r in sorted(risks_b - risks_a)] + [f"− Risk: {r}" for r in sorted(risks_a - risks_b)]
    return out


def open_pr_here(conn: sqlite3.Connection, cid: str, upstream: str) -> int | None:
    """The same rule ``ProposalService.start`` audits as ``open_pr_override`` (one definition)."""
    row = db.one(conn, "SELECT pr_number FROM contributions WHERE upstream = ? AND id != ? AND pr_number IS NOT NULL"
                 " AND state IN ('SUBMITTED','WATCHING') ORDER BY updated_at DESC LIMIT 1",
                 (upstream, cid))  # fmt: skip
    return int(row["pr_number"]) if row else None


def forbidden_hits(ctx: AppContext, areas: list[str]) -> list[str]:
    globs = ctx.config.policy.diff.forbidden_paths
    return [a for a in areas if any(fnmatch.fnmatch(a, g) or fnmatch.fnmatch(a.rstrip("/") + "/x", g) for g in globs)]


# --------------------------------------------------------------------------- open discovery (design §9)
def discovered_of(ctx: AppContext, upstream: str) -> DiscoveredView | None:
    """The ``discovered_targets`` row of a repo outside targets.yaml, or None (config targets, which
    keep their card byte-identical, and repos without a row)."""
    if resolve_target(ctx.config.targets, upstream) is not None:
        return None
    return ctx.discovered.get(upstream)


def keepable(found: DiscoveredView | None) -> bool:
    """The ⭐ Keep / 🚫 Never row is shown only for a discovered ``candidate`` (§9.2)."""
    return found is not None and found.status == "candidate"


def discovery_line(found: DiscoveredView) -> k.Line:
    """``🔎 open discovery · topic gitops · ★ 24,440`` (search), ``🔎 suggested by Hermes · ★ N`` or
    ``⭐ kept target (open discovery) · ★ N``; unknown parts are left out. The topic is escaped text,
    the stars come from the integer column (§9.1)."""
    spans: list[k.Span]
    if found.source == "owner" or found.status == "kept":
        spans = [k.Const("⭐ kept target (open discovery)")]
    elif found.source == "hermes":
        spans = [k.Const("🔎 suggested by Hermes")]
    else:
        spans = [k.Const("🔎 open discovery")]
        if found.topic:
            spans += [k.Const(" · topic "), k.Text(found.topic, 50)]
    if found.stars is not None:
        spans.append(k.Const(f" · ★ {found.stars:,}"))
    return tuple(spans)


def activity_line(found: DiscoveredView | str | None, now: datetime) -> k.Line | None:
    """Owner addition (2026-10-21): ``🌿 last commit on the default branch: 3 d ago`` from the stored
    default-branch commit date (the discovered row's, or vet's ISO timestamp for a seed repo); None
    when it is unknown."""
    when = found.default_branch_commit_at if isinstance(found, DiscoveredView) else found
    if not when or not isinstance(when, str):
        return None
    try:
        days = max(0, (now - from_iso(when)).days)
    except ValueError:
        return None
    return (k.Const(f"🌿 last commit on the default branch: {'today' if days == 0 else f'{days} d ago'}"),)


# --------------------------------------------------------------------------- the card
def card_view(ctx: AppContext, row: sqlite3.Row) -> k.View | None:
    cid, version = str(row["cid"]), int(row["version"])
    prop = load(ctx.conn, cid, version)
    contrib = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
    if prop is None or contrib is None:
        return None
    user = contrib["source"] == "user"
    vet = _json(contrib["vetting_json"])
    head = k.header(icons.PROPOSAL, "Proposal", f"{cid} · v{version}",
                    k.mode_badge(db.current_dry_run(ctx.conn), na="local change"))  # fmt: skip
    lines: list[k.Line] = [head]
    if user:
        note: list[k.Span] = [k.Const(f"{icons.USER} "), k.Bold(k.Const("requested by you"))]
        if contrib["notes"]:
            note += [k.Const(' · "'), k.Text(str(contrib["notes"]), 80), k.Const('"')]
        lines.append(tuple(note))
    lines.append((k.Bold(k.Text(str(contrib["upstream"]), 150)),))
    found = discovered_of(ctx, str(contrib["upstream"]))
    if found is not None:
        lines.append(discovery_line(found))
    # discovered rows carry the date; seed repos get vet's read of it (owner addition 2026-10-21)
    activity = activity_line(found if found is not None and found.default_branch_commit_at
                             else vet.get("default_branch_commit_at"), ctx.clock.now())  # fmt: skip
    if activity is not None:
        lines.append(activity)
    if contrib["issue_number"]:
        issue: list[k.Span] = [k.Const("Issue ")]
        number = k.Const(f"#{contrib['issue_number']}")
        url = str(contrib["issue_url"] or "")
        issue.append(k.Link(number, url) if url.startswith("https://github.com/") else number)
        if contrib["issue_title"]:
            issue += [k.Const(" "), k.Bold(k.Text(str(contrib["issue_title"]), 200))]
        issue.append(k.Const(f" · {kind_name(prop.get('kind'))}"))
        lines.append(tuple(issue))
    blocks: list[k.Block] = [k.Block((k.Lines(tuple(lines)),), name="header")]
    blocks += _diff_block(ctx, cid, version, prop)
    blocks.append(k.Block((k.claim("Problem", str(prop.get("problem", "")), max_len=300),), name="problem"))
    steps = list(prop.get("approach") or [])
    first = [(k.Const(f"  {i}. "), k.Text(s, 100)) for i, s in enumerate(steps[:APPROACH_REQUIRED], 1)]
    blocks.append(k.Block((k.Lines(((k.Const(f"{icons.MODEL} "), k.Bold(k.Const("Approach"))), *first)),),
                          name="approach"))  # fmt: skip
    later = enumerate(steps[APPROACH_REQUIRED:APPROACH_MAX], APPROACH_REQUIRED + 1)
    rest: list[k.Line] = [(k.Const(f"  {i}. "), k.Text(s, 100)) for i, s in later]
    if len(steps) > APPROACH_MAX:
        rest.append(k.line(f"  +{len(steps) - APPROACH_MAX} more"))
    if rest:
        blocks.append(k.Block((k.Lines(tuple(rest)),), required=False, priority=3, name="approach_rest"))
    areas = list(prop.get("areas") or [])
    if areas:
        shown: list[k.Span] = [k.Const(f"{icons.MODEL} "), k.Bold(k.Const("Likely files")), k.Const(" ")]
        for i, a in enumerate(areas[:FILES_MAX]):
            shown += [k.Const(" · ") if i else k.Const(""), k.Code(k.Text(middle_cut(a, 60), 60))]
        if len(areas) > FILES_MAX:
            shown.append(k.Const(f" +{len(areas) - FILES_MAX} more"))
        blocks.append(k.Block((k.Lines((tuple(shown),)),), required=False, priority=4, name="files"))
    validate = _validate_line(ctx, str(contrib["upstream"]), vet, prop)
    blocks.append(k.Block((k.Lines((_size_line(ctx, contrib, prop, user), validate)),), name="size"))
    repo_policy = policy_line(vet)
    if repo_policy is not None:
        blocks.append(k.Block((k.Lines((repo_policy,)),), required=False, priority=9, name="repo_policy"))
    facts = _signals(vet)
    if facts:
        blocks.append(k.Block((k.Lines(((k.Const("✅ Signals: "), k.Text(" · ".join(facts), 200)),)),),
                              required=False, priority=1, name="signals"))  # fmt: skip
    warnings = _warnings(ctx, contrib, vet, areas, user, prop)
    if warnings:
        blocks.append(k.Block((k.Lines(tuple(warnings)),), name="warnings"))
    if prop.get("rationale"):
        blocks.append(k.Block((k.claim("Why it fits", str(prop["rationale"]), max_len=160),), required=False,
                              priority=2, name="rationale"))  # fmt: skip
    conf = float(prop.get("confidence", 0.0))
    tail = (f"Confidence {icons.MODEL} {conf:.2f} · Risks {icons.MODEL} {len(prop.get('risks') or [])}"
            f" · Alternatives {icons.MODEL} {len(prop.get('alternatives') or [])} → 🧭 Details")  # fmt: skip
    blocks.append(k.Block((k.Lines((k.line(tail),)),), required=False, priority=5, name="confidence"))
    when = from_iso(row["expires_at"])
    expiry = (k.Const("⏳ Proposal expires "), k.Time(int(when.timestamp()), when.strftime("%d %b %H:%M")))
    blocks.append(k.Block((k.Lines((expiry,)),), name="expiry"))
    return k.View(kind="proposal", blocks=blocks, cid=cid, rev=version)


def _diff_block(ctx: AppContext, cid: str, version: int, prop: dict[str, Any]) -> list[k.Block]:
    if version < 2:
        return []
    old = load(ctx.conn, cid, version - 1)
    if old is None:
        return []
    diff = version_diff(old, prop)
    lines: list[k.Line] = [(k.Bold(k.Const(f"Changed since v{version - 1}")),
                            k.Const(" (diff by gatekeeper; content 🤖):"))]  # fmt: skip
    if prop.get("_feedback"):
        lines.append((k.Const('You asked: "'), k.Text(str(prop["_feedback"]), 80), k.Const('"')))
    if not diff:
        lines.append(k.line("No structural change (wording only)"))
    for d in diff[:DIFF_LINES]:
        lines.append((k.Text(d, DIFF_LINE_MAX),))
    if len(diff) > DIFF_LINES:
        lines.append(k.line(f"+{len(diff) - DIFF_LINES} more → 🧭 Details"))
    return [k.Block((k.Lines(tuple(lines)),), required=False, priority=6, name="version_diff")]


def _size_line(ctx: AppContext, contrib: sqlite3.Row, prop: dict[str, Any], user: bool) -> k.Line:
    est = prop.get("estimate") or {}
    cls = str(prop.get("_class") or est.get("class") or "?")
    fit = "👤 size preference not applied"
    if not user:
        cap = ctx.discovery_prefs.effective(str(contrib["upstream"])).autonomous_max_class
        fit = f"✅ fits your auto size ≤ {cap}"
    numbers = (f" · ~{est.get('code_lines', 0)} code · ~{est.get('test_lines', 0)} test · {est.get('files', 0)} files"
               f" · {fit}")  # fmt: skip
    return (k.Const(f"Size {icons.MODEL} est "), k.Bold(k.Const(cls)), k.Const(numbers))


def _validate_line(ctx: AppContext, upstream: str, vet: dict[str, Any], prop: dict[str, Any]) -> k.Line:
    """✅ the gatekeeper's facts (the vet's test runner when it found one, the validation tier cap),
    then 🤖 the scout's test count and plan (§14.11.15 field rules)."""
    facts: list[k.Span] = [k.Const("Validate ✅ ")]
    runner = vet.get("test_runner")
    if isinstance(runner, str) and runner:
        facts += [k.Const("repo has tests ("), k.Text(runner, 30), k.Const(") · ")]
    tier = ctx.discovery_prefs.effective(upstream).filters.validation_tier_max
    facts.append(k.Const(f"tiers ≤ {tier} · "))
    tests = prop.get("tests") or []
    what = f"{len(tests)} test(s) to add/update" if tests else "no suite"
    return (*facts, k.Const(f"{icons.MODEL} {what} · "), k.Text(str(prop.get("validation_plan") or "—"), 120))


def _signals(vet: dict[str, Any]) -> list[str]:
    out: list[str] = []
    claim = vet.get("claim_status")
    if claim == "unclaimed":
        out.append("no competing claim")
    elif claim == "stale_claim":
        out.append("stale claim by someone else")
    if vet.get("requires_assignment"):
        out.append("assignment required")
    elif vet:
        out.append("assignment not required")
    return out[:3]


def policy_line(vet: dict[str, Any]) -> k.Line | None:
    """``✅ AI policy: not stated · CI on PRs: ✅ tests, lint (8/10 merged PRs)`` (§9.4, §9.5, §14.11.15):
    the gatekeeper's fixed wording for the stored status, condition kinds and CI facts (closed sets and
    counts, never repo text; check names are in 🧭 Details · Validation); None for a never-vetted row."""
    if not vet:
        return None
    text = ai_policy.display(vet.get("ai_policy"), ai_policy.parse_conditions(vet.get("ai_conditions")))
    ci = ci_facts.display(ci_facts.from_stored(vet))
    return (k.Const("✅ "), k.Bold(k.Const("AI policy")), k.Const(f": {text} · "), k.Bold(k.Const("CI on PRs")),
            k.Const(f": {ci}"))  # fmt: skip


def code_not_inspected(prop: dict[str, Any]) -> bool:
    """§14.11.15: the runner measured no read of ``/work/repo`` in the scout's session, or the scout
    said it could not read the code. Legacy versions without ``code_inspection`` show nothing."""
    seen = prop.get("code_inspection")
    if not isinstance(seen, dict):
        return False
    return seen.get("repo_reads") == 0 or seen.get("self_reported_unread") is True


def _warnings(ctx: AppContext, contrib: sqlite3.Row, vet: dict[str, Any], areas: list[str],
              user: bool, prop: dict[str, Any]) -> list[k.Line]:  # fmt: skip
    out: list[k.Line] = []
    if code_not_inspected(prop):
        out.append(k.line("⚠ code not inspected"))
    hits = forbidden_hits(ctx, areas)
    if hits:
        # the paths are scout text (🤖): made visible and defused, in code style (U6, U8)
        hit: list[k.Span] = [k.Const(f"⛔ Touches forbidden paths {icons.MODEL} ")]
        for i, h in enumerate(hits[:3]):
            hit += [k.Const(", ") if i else k.Const(""), k.Code(k.Text(middle_cut(h, 40), 40))]
        out.append(tuple(hit))
    if vet.get("cla_required"):
        out.append(k.line("⚠️ CLA required"))
    pr = open_pr_here(ctx.conn, str(contrib["id"]), str(contrib["upstream"])) if user else None
    if pr is not None:
        out.append(k.line(f"⚠️ You already have PR #{pr} open here"))
    return out


def render(ctx: AppContext, row: sqlite3.Row) -> CardMessages:
    """``ProposalApprovals.renderer``: the classic card text (no parts)."""
    view = card_view(ctx, row)
    if view is None:
        return CardMessages(card=f"<b>💡 Proposal</b> · {ids.html(str(row['cid']))} · v{int(row['version'])} ·"
                                 " (content unavailable)")  # fmt: skip
    try:
        return CardMessages(card=classic.render(view, surface="card").text)
    except k.ViewTooLarge:
        return CardMessages(card=ids.autocode(f"⚠️ Proposal card too large to render (bug) · {row['cid']}"),
                            too_large=True)  # fmt: skip


# --------------------------------------------------------------------------- keyboard
def startable(ctx: AppContext, cid: str, version: int) -> bool:
    prop = load(ctx.conn, cid, version)
    return prop is not None and not forbidden_hits(ctx, list(prop.get("areas") or []))


def kit_token(ctx: AppContext, purpose: str, *, message_id: int, cid: str, arg: dict[str, Any],
              single_use: bool = False, ttl: timedelta = KIT_TTL) -> str:  # fmt: skip
    return ctx.approvals.mint_token(purpose, chat_id=ctx.telegram_chat_id, message_id=message_id,
                                    expires_at=ctx.clock.now() + ttl, approval_id=None, cid=cid,
                                    arg=json.dumps(arg, sort_keys=True), single_use=single_use)  # fmt: skip


def card_rows(ctx: AppContext, approval_id: str, tokens: dict[str, str]) -> keyboards.Rows:
    """``ProposalApprovals.card_rows``: the decision token plus fresh kit tokens on the card message."""
    row = db.one(ctx.conn, "SELECT * FROM proposal_approvals WHERE id = ?", (approval_id,))
    assert row is not None
    cid, version, mid = str(row["cid"]), int(row["version"]), int(row["tg_message_id"])
    arg = {"cid": cid, "version": version}
    kit = {p: kit_token(ctx, p, message_id=mid, cid=cid, arg=arg) for p in PROPOSAL_PURPOSES}
    prop = load(ctx.conn, cid, version) or {}
    start: keyboards.Button = (("✅ Start work", tokens["proposal_start"], "success") if startable(ctx, cid, version)
                               else ("⛔ Not startable", keyboards.DISABLED))  # fmt: skip
    rows: keyboards.Rows = [[start, ("⏭ Skip", kit["proposal_skip"])]]
    second: list[keyboards.Button] = [("✏️ Adjust plan", kit["proposal_adjust"])]
    cls = str(prop.get("_class") or "xs")
    if cls in SIZE_CLASSES and SIZE_CLASSES.index(cls) > 0:
        second.append(("📏 Make it smaller", kit["proposal_smaller"]))
    rows.append(second)
    rows.append([("🚫 Never…", kit["proposal_never"]), ("⏰ Later…", kit["proposal_later"])])
    fourth: list[keyboards.Button] = [("🧭 Details", kit["proposal_details"])]
    contrib = db.one(ctx.conn, "SELECT vetting_json, upstream FROM contributions WHERE id = ?", (cid,))
    if contrib is not None and _json(contrib["vetting_json"]).get("requires_assignment"):
        fourth.append(("🙋 Ask maintainers first", kit["proposal_ask_first"]))
    rows.append(fourth)
    if contrib is not None and keepable(discovered_of(ctx, str(contrib["upstream"]))):
        # §9.2: minted only for a discovered candidate; Never lands on the existing confirm step
        keep = kit_token(ctx, "proposal_keep", message_id=mid, cid=cid, arg=arg)
        never = kit_token(ctx, "proposal_never_pick", message_id=mid, cid=cid, arg={**arg, "target": "repo"})
        rows.append([("⭐ Keep as target", keep), ("🚫 Never this repo", never)])
    return rows


# --------------------------------------------------------------------------- Confirm (S1 two taps)
def confirm_panel(ctx: AppContext, approval_id: str, confirm: str, cancel: str) -> tuple[str, keyboards.Rows]:
    """The Start-work Confirm panel: the card, the bound sentence, the CLA and one-open-PR warnings."""
    svc = ctx.decisions.proposals
    row = svc._row(approval_id)
    binding = json.loads(row["binding_json"])
    contrib = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (row["cid"],))
    assert contrib is not None
    lines = [svc.card_view(approval_id).card, "", f"<b>{esc(svc.confirm_text(binding))}</b>"]
    vet = _json(contrib["vetting_json"])
    label = "🛠 Confirm start"
    if vet.get("cla_required"):
        lines.append("⚠️ CLA required by this repo — confirming means you have signed it")
        label = "🛠 Confirm (CLA signed)"
    pr = open_pr_here(ctx.conn, str(contrib["id"]), str(contrib["upstream"])) if contrib["source"] == "user" else None
    if pr is not None:
        lines.append(f"⚠️ You already have PR #{pr} open here — confirming overrides the limit for this item")
    return "\n".join(lines), [[(label, confirm, "primary"), ("↩️ Cancel", cancel)]]


# --------------------------------------------------------------------------- Details
DETAIL_SECTIONS = ("Problem & approach", "Files & tests", "Validation", "Risks & alternatives", "Overview",
                   "Version diff")  # fmt: skip


def _ci_details(ctx: AppContext, cid: str) -> list[k.Element]:
    """``✅ CI on PRs`` with the check names seen (repo text: escaped ``Text`` ≤ 40 each)."""
    contrib = db.one(ctx.conn, "SELECT vetting_json FROM contributions WHERE id = ?", (cid,))
    vet = _json(contrib["vetting_json"]) if contrib is not None else {}
    if not vet:
        return []
    facts = ci_facts.from_stored(vet)
    lines: list[k.Line] = [(k.Const("✅ "), k.Bold(k.Const("CI on PRs")), k.Const(f": {ci_facts.display(facts)}"))]
    if facts.checks:
        label = "Checks seen on merged PRs" if facts.source == "merged_prs" else "Workflow jobs"
        names: list[k.Span] = [k.Const(f"{label}: ")]
        for i, name in enumerate(facts.checks):
            names += [k.Const(" · ") if i else k.Const(""), k.Code(k.Text(name, ci_facts.CHECK_NAME_MAX))]
        lines.append(tuple(names))
    return [k.Lines(tuple(lines))]


def details_view(ctx: AppContext, cid: str, version: int, section: int = 1) -> k.View:
    """``🧭 Details`` (kind ``proposal_details``): section navigation and no decision buttons."""
    prop = load(ctx.conn, cid, version) or {}
    section = min(max(1, section), len(DETAIL_SECTIONS))
    crumb = (k.Const("🧭 "), k.Bold(k.Const(f"{cid} v{version}")),
             k.Const(f" · {section}/{len(DETAIL_SECTIONS)} {DETAIL_SECTIONS[section - 1]}"))  # fmt: skip
    elements: list[k.Element] = []
    if section == 1:
        elements.append(k.claim("Problem", str(prop.get("problem", "")), max_len=2000))
        elements += [k.claim(f"Step {i}", s, max_len=300) for i, s in enumerate(prop.get("approach") or [], 1)]
    elif section == 2:
        elements += [k.Lines(tuple((k.Const("• "), k.Code(k.Text(middle_cut(a, 80), 80))) for a in
                                   (prop.get("areas") or [])[:30]) or (k.line("No files named."),))]  # fmt: skip
        elements += [k.claim("Test", t, max_len=300) for t in (prop.get("tests") or [])[:20]]
        if prop.get("no_suite_reason"):
            elements.append(k.claim("No suite", str(prop["no_suite_reason"]), max_len=300))
    elif section == 3:
        elements.append(k.claim("Validation plan", str(prop.get("validation_plan") or "—"), max_len=1000))
        elements += _ci_details(ctx, cid)
    elif section == 4:
        elements += [k.claim("Risk", r, max_len=200) for r in (prop.get("risks") or [])[:10]]
        elements += [k.claim("Alternative", str(a.get("summary", "")), max_len=200)
                     for a in (prop.get("alternatives") or [])[:10] if isinstance(a, dict)]  # fmt: skip
        if len(elements) == 0:
            elements.append(k.Lines((k.line("No risks or alternatives listed."),)))
    elif section == 5:
        elements.append(k.claim("Overview", str(prop.get("overview") or "—"), max_len=3000))
    else:
        old = load(ctx.conn, cid, version - 1) if version > 1 else None
        diff = version_diff(old, prop) if old is not None else []
        text = "\n".join(diff) or ("No structural change (wording only)" if old is not None else "First version.")
        elements.append(k.PreBlock(text[:3000], language="diff"))
    # each element is its own optional block, dropped from the end when the section is too long (AC 53)
    blocks = [k.Block((k.Lines((crumb,)),), name="crumb")]
    blocks += [k.Block((e,), required=False, priority=1000 - i, name=f"e{i}") for i, e in enumerate(elements)]
    arg = {"cid": cid, "version": version}
    rows: k.Rows = [k.pager("proposal_details_nav", arg, section, len(DETAIL_SECTIONS))]
    return k.View(kind="proposal_details", blocks=blocks, rows=rows, cid=cid, rev=version, page=section)
