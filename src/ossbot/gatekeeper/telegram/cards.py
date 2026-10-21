"""Approval cards: the decorated S1 layout (DESIGN §14.11.1, §14.4, §14.4a; PLAN item 45).

Rendered only from gatekeeper data in the DB (derived numstat and size report, derived commit
message, stored effective title/body, policy and validation records) through the UI kit:

* ✅ lines are gatekeeper facts; 🤖 lines are model claims, quoted (U6). Untrusted strings (issue
  titles, model text) are redacted, made visible and defused (U8, U14).
* **The PR title is publish bytes**: shown in full in ``<code>`` (escaped, made visible, never cut,
  defused or redacted) and only ``scan()``ed. A hit (a bug: packaging already refused it) withholds
  the whole card (``CardMessages.withheld``; the publisher invalidates the approval ``render_failed``).
* The kit's budget rule replaces the old tail cut: required blocks (header, mode, subject, the PR
  title, the size line and its ⚠️, the expiry) are never cut; optional ones are dropped lowest
  priority first. ``ViewTooLarge`` is a bug and yields the fixed stub (``too_large``).

Messages 2..K+1 carry the bytes that will be published verbatim, in escaped ``<pre>`` parts
(``body_parts_total`` = their number); their kinds (``body_part``/``commit_msg``) are recorded for
``tg_views`` so S12 can stub them.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ossbot.common import ai_policy
from ossbot.common.clock import from_iso
from ossbot.common.gitcmd import NumstatEntry
from ossbot.common.schemas import OutboundKind
from ossbot.gatekeeper import db
from ossbot.gatekeeper.telegram.escape import b, esc, make_visible
from ossbot.gatekeeper.telegram.review.size_line import size_lines
from ossbot.gatekeeper.telegram.ui import classic, icons
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import redact as ui_redact
from ossbot.gatekeeper.telegram.ui.limits import CLASSIC_MAX, PART_MAX, chunk_verbatim

MESSAGE_MAX = CLASSIC_MAX  # escaped characters per message (Bot API limit 4096)
NUMSTAT_LINES = 15
YOU_ASKED_MAX = 5
WITHHELD = "⚠️ Card withheld: it matched a secret pattern (bug) · {cid} r{rev} · see /logs {cid}"
TOO_LARGE = "⚠️ Card too large to render (bug) · {cid} r{rev} · nothing can be approved from it; see /logs {cid}"
HEADERS: dict[str, tuple[str, str]] = {
    "open_pr": (icons.PR, "Ready for review"),
    "push_update": (icons.REVISION, "Fix ready for PR #{pr}"),
    "comment": (icons.COMMENT, "Comment ready"),
    "issue_claim": (icons.CLAIM, "Issue claim ready"),
}


@dataclass(frozen=True)
class AskedItem:
    """One "You asked" line of a revised card (S6)."""

    where: str
    text: str
    touched: bool | None = None  # None = unknown (interdiff unavailable)
    coder: str = ""


@dataclass(frozen=True)
class CardView:
    kind: OutboundKind
    cid: str
    upstream: str
    dry_run: bool
    title: str | None = None
    body: str | None = None
    commit_message: str | None = None
    numstat: tuple[NumstatEntry, ...] = ()
    head_sha: str | None = None
    round_base: str | None = None
    base_ref: str | None = None
    identity: str | None = None
    branch: str | None = None
    fork_owner: str | None = None
    pr_number: int | None = None
    target_number: int | None = None
    edited: bool = False
    # S1 decoration (PLAN item 45); every field optional so old rows still render
    rev: int | None = None
    issue_number: int | None = None
    issue_title: str | None = None
    issue_url: str | None = None
    user_directed: bool = False
    notes: str = ""
    size_report: dict[str, Any] | None = None
    validation: dict[str, Any] = field(default_factory=dict)
    review: dict[str, Any] = field(default_factory=dict)
    models: dict[str, Any] = field(default_factory=dict)
    risk: str | None = None
    confidence: float | None = None
    rationale: str | None = None
    expires_at: datetime | None = None
    interdiff: dict[str, Any] | None = None  # derived/<cid>/<rev>/interdiff.json
    asked: tuple[AskedItem, ...] = ()
    asked_total: int = 0
    vetting: dict[str, Any] = field(default_factory=dict)  # vetting_json: the repo_policy line (open_pr)


@dataclass(frozen=True)
class CardMessages:
    card: str
    parts: list[str] = field(default_factory=list)
    part_kinds: list[str] = field(default_factory=list)  # "body_part" | "commit_msg" per part
    withheld: bool = False  # a verbatim scan hit (bug): the publisher sends the stub, render_failed
    too_large: bool = False  # ViewTooLarge (bug): same handling


def _chunks(text: str, limit: int = PART_MAX) -> list[str]:
    return chunk_verbatim(text, limit)


def verbatim_parts(label: str, text: str) -> list[str]:
    chunks = _chunks(text)
    total = len(chunks)
    return [f"{b(f'{label} ({i}/{total})')}\n<pre>{esc(make_visible(c)[0])}</pre>" for i, c in enumerate(chunks, 1)]


def _mode(dry_run: bool) -> k.Span:
    return k.mode_badge(dry_run)


def _unix(when: datetime) -> int:
    return int(when.timestamp())


# ------------------------------------------------------------------------------------ blocks
def _header(view: CardView) -> k.Line:
    icon, title = HEADERS[view.kind]
    if view.kind == "open_pr" and view.interdiff is not None:
        icon, title = icons.REVISION, "Revised for review"
    title = title.format(pr=view.pr_number)
    ident = view.cid + (f" · r{view.rev}" if view.rev and view.kind in {"open_pr", "push_update"} else "")
    return k.header(icon, title, ident, _mode(view.dry_run))


def _subject(view: CardView) -> list[k.Line]:
    lines: list[k.Line] = []
    if view.user_directed:
        note: list[k.Span] = [k.Const(f"{icons.USER} "), k.Bold(k.Const("requested by you"))]
        if view.notes:
            note += [k.Const(' · Notes: "'), k.Text(view.notes, 80), k.Const('"')]
        lines.append(tuple(note))
    if view.kind in {"comment", "issue_claim"}:
        lines.append((k.Const("Target: "), k.Bold(k.Text(f"{view.upstream}#{view.target_number}", 150))))
        return lines
    repo: list[k.Span] = [k.Bold(k.Text(view.upstream, 150))]
    if view.kind == "push_update":
        repo += [k.Const(" · incremental commit on "), k.Code(k.Text(f"{view.fork_owner}:{view.branch}", 120))]
    lines.append(tuple(repo))
    if view.issue_number:
        issue: list[k.Span] = [k.Const("Issue ")]
        number = k.Const(f"#{view.issue_number}")
        issue.append(k.Link(number, view.issue_url) if view.issue_url else number)
        if view.issue_title:
            issue += [k.Const(" "), k.Bold(k.Text(view.issue_title, 200))]
        lines.append(tuple(issue))
    return lines


def _facts(view: CardView) -> list[k.Line]:
    added = sum(e.added or 0 for e in view.numstat)
    deleted = sum(e.deleted or 0 for e in view.numstat)
    facts: list[k.Line] = []
    risk = ""
    if view.risk:
        risk = f"Risk 🤖 {view.risk}" + (f" ({view.confidence:.2f})" if view.confidence is not None else "") + " · "
    cmds = [c for c in view.validation.get("commands", []) if isinstance(c, dict)]
    if cmds:
        tests = " · ".join(f"{'✅' if c.get('exit_code') == 0 else '❌'} {str(c.get('name', '?'))[:20]}"
                           for c in cmds[:4])  # fmt: skip
    else:
        tests = "relies on upstream CI"
    facts.append((k.Const(f"{risk}Change ✅ {len(view.numstat)} files +{added} −{deleted} · Tests "),
                  k.Text(tests, 200)))  # fmt: skip
    return facts


def _review_claim(view: CardView) -> k.Block | None:
    if not view.review:
        return None
    verdict = str(view.review.get("verdict", "?")).upper()
    blocking = len(view.review.get("blocking") or [])
    nits = len(view.review.get("nits") or [])
    distinct = ""
    if view.models.get("reviewer") and view.models.get("coder"):
        same = view.models["reviewer"] == view.models["coder"]
        distinct = " (reviewer = coder ⚠️)" if same else " (reviewer ≠ coder ✅)"
    text = f"Self-review 🤖 {verdict}{distinct} · {blocking} blocking · {nits} nits"
    return k.Block((k.Lines(((k.Text(text, 200),),)),), required=False, priority=3, name="self_review")


def _asked_block(view: CardView) -> list[k.Block]:
    """S6 "You asked" (≤ 5 items of ≤ 80 chars, then "+N more") and the interdiff summary, both optional."""
    out: list[k.Block] = []
    if view.asked:
        lines: list[k.Line] = [(k.Bold(k.Const("You asked")), k.Const(f" ({view.asked_total} comments):"))]
        for i, item in enumerate(view.asked[:YOU_ASKED_MAX], 1):
            flag = "" if item.touched is None else (" · ✅ touched" if item.touched else " · ⚠️ not touched")
            parts: list[k.Span] = [k.Const(f"{i}. "), k.Text(item.where, 40), k.Const(" — "), k.Text(item.text, 80),
                                   k.Const(flag)]  # fmt: skip
            if item.coder:
                parts += [k.Const(" · 🤖 coder: "), k.Text(item.coder, 60)]
            lines.append(tuple(parts))
        if view.asked_total > YOU_ASKED_MAX:
            lines.append(k.line(f"+{view.asked_total - YOU_ASKED_MAX} more → 🧭 Explore · 9 PR text"))
        out.append(k.Block((k.Lines(tuple(lines)),), required=False, priority=0, name="you_asked"))
    if view.interdiff is not None:
        info = view.interdiff
        if info.get("mode") == "unavailable":
            line = k.line(f"Interdiff unavailable ({str(info.get('reason', 'error'))[:60]}) — review the full diff")
        else:
            files = info.get("files") or []
            shown = " · ".join(f"{f.get('path', '?')} +{f.get('added', 0)} −{f.get('deleted', 0)}" for f in files[:4])
            how = ("base unchanged" if info.get("mode") == "interdiff"
                   else f"base moved {str(info.get('old_base', ''))[:7]} → {str(info.get('new_base', ''))[:7]};"
                        " line-level touch check unavailable")  # fmt: skip
            line = (k.Bold(k.Const(f"What changed since r{(view.rev or 2) - 1}")), k.Const(f" ✅ ({how}): "),
                    k.Text(shown or "no file changes", 300))  # fmt: skip
        full = k.line("Full change vs base: you approve the full patch, not the interdiff")
        out.append(k.Block((k.Lines((line, full)),), required=False, priority=2, name="interdiff"))
    return out


def card_view_kit(view: CardView, *, body_part_count: int = 0) -> k.View:
    """The S1 card as a kit view (classic only: approval cards are never rich, U3)."""
    blocks: list[k.Block] = [k.Block((k.Lines((_header(view), *_subject(view))),), name="header")]
    blocks += _asked_block(view)
    if view.rationale:
        blocks.append(k.Block((k.claim("Why", view.rationale, max_len=200),), required=False, priority=4, name="why"))
    if view.kind in {"open_pr", "push_update"}:
        blocks.append(k.Block((k.Lines(tuple(_facts(view))),), required=False, priority=5, name="facts"))
        size = size_lines(view.size_report, coder_reason=str(view.review.get("size_reason") or "") or None)
        blocks.append(k.Block((k.Lines(size.lines),), name="size"))
        review = _review_claim(view)
        if review is not None:
            blocks.append(review)
        short, base = (view.head_sha or "")[:7], (view.round_base or "")[:7]
        on = f"{view.base_ref}@{base}" if view.kind == "open_pr" else f"{base} (current PR head)"
        commit = (k.Const("Commit "), k.Code(k.Const(short)), k.Const(" on "), k.Text(on, 120), k.Const(" · "),
                  k.Text(view.identity or "", 160))  # fmt: skip
        target = f"{view.fork_owner}/" + view.upstream.split("/", 1)[-1]
        branch = (k.Const("Branch "), k.Code(k.Text(view.branch or "", 120)), k.Const(" → "), k.Text(target, 150))
        blocks.append(k.Block((k.Lines((commit, branch)),), required=False, priority=6, name="commit"))
    repo_policy = _repo_policy_block(view)
    if repo_policy is not None:
        blocks.append(repo_policy)
    if view.kind == "open_pr":
        title: list[k.Span] = [k.Const("PR title: "), k.Code(k.Verbatim(view.title or "", "pr_title"))]
        if view.edited:
            title.append(k.Const("   [edited by you]"))
        body = view.body or ""
        text_lines: list[k.Line] = [tuple(title), k.line(
            f"Body {len(body):,} chars ({body_part_count} part(s) ↓) · commit message ↓")]  # fmt: skip
        blocks.append(k.Block((k.Lines(tuple(text_lines)),), name="pr_text"))
    elif view.kind == "push_update":
        blocks.append(k.Block((k.Lines((k.line("Commit message ↓ (the bytes that will be pushed)"),)),),
                              required=False, priority=7, name="pr_text"))  # fmt: skip
    else:
        blocks.append(k.Block((k.Lines((k.line(f"Body: {len(view.body or '')} chars, "
                                               f"{body_part_count} part(s) below"),)),), name="body"))  # fmt: skip
    _, hidden = make_visible((view.body or "") + (view.commit_message or "") + (view.title or ""))
    if hidden:
        note = k.line(f"⚠️ {hidden} invisible/bidi/control character(s) shown as ⟦U+XXXX⟧")
        blocks.append(k.Block((k.Lines((note,)),), name="hidden"))
    if view.expires_at is not None:
        when = view.expires_at
        expiry = (k.Const("⏳ Approval expires "), k.Time(_unix(when), when.strftime("%d %b %H:%M")))
        blocks.append(k.Block((k.Lines((expiry,)),), name="expiry"))
    return k.View(kind="card", blocks=blocks, cid=view.cid, rev=view.rev)


def _verbatim_hit(view: CardView) -> bool:
    """Publish bytes are scanned, never altered (U14): the title and the bytes of every part."""
    hits = ui_redact.scan(view.title or "", surface="pr_title")
    for text in (view.body or "", view.commit_message or ""):
        hits += ui_redact.scan(text, surface="card_part")
    return hits > 0


def render(view: CardView) -> CardMessages:
    parts: list[str] = []
    kinds: list[str] = []
    if view.kind == "open_pr":
        body_parts = verbatim_parts("PR body", view.body or "")
        parts += body_parts
        kinds += ["body_part"] * len(body_parts)
    elif view.kind in {"comment", "issue_claim"}:
        body_parts = verbatim_parts("Comment", view.body or "")
        parts += body_parts
        kinds += ["body_part"] * len(body_parts)
    if view.kind in {"open_pr", "push_update"}:
        commit = verbatim_parts("Commit message", view.commit_message or "")
        parts += commit
        kinds += ["commit_msg"] * len(commit)
    rev = view.rev if view.rev is not None else "?"
    if _verbatim_hit(view):
        return CardMessages(card=esc(WITHHELD.format(cid=view.cid, rev=rev)), withheld=True)
    count = kinds.count("body_part")
    try:
        out = classic.render(card_view_kit(view, body_part_count=count), surface="card")
    except k.ViewTooLarge:
        return CardMessages(card=esc(TOO_LARGE.format(cid=view.cid, rev=rev)), too_large=True)
    return CardMessages(card=out.text, parts=parts, part_kinds=kinds)


# ----------------------------------------------------------------------------- DB views
def _numstat(contrib: sqlite3.Row) -> tuple[NumstatEntry, ...]:
    raw: list[dict[str, Any]] = json.loads(contrib["diffstat_json"] or "[]")
    return tuple(NumstatEntry(d.get("added"), d.get("deleted"), str(d.get("path", ""))) for d in raw)


def _json(value: Any) -> dict[str, Any]:
    try:
        doc = json.loads(value) if value else {}
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


_DERIVED_ROOT: dict[str, Path] = {}


def set_derived_root(path: Path) -> None:
    """``<gatekeeper_dir>/derived`` (set once by ``build_context``): where the interdiff files live."""
    _DERIVED_ROOT["root"] = path


def interdiff_info(cid: str, rev: int | None) -> dict[str, Any] | None:
    """``derived/<cid>/<rev>/interdiff.json`` when present (``gatekeeper/interdiff.py``), else None."""
    root = _DERIVED_ROOT.get("root")
    if root is None or rev is None or rev < 2:
        return None
    try:
        doc = json.loads((root / cid / str(rev) / "interdiff.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def view_for_action(conn: sqlite3.Connection, action_id: str, dry_run: bool,
                    expires_at: datetime | None = None) -> CardView:  # fmt: skip
    action = db.one(conn, "SELECT * FROM outbound_actions WHERE id = ?", (action_id,))
    if action is None:
        raise KeyError(action_id)
    contrib = db.one(conn, "SELECT * FROM contributions WHERE id = ?", (action["cid"],))
    if contrib is None:
        raise KeyError(action["cid"])
    payload: dict[str, Any] = json.loads(action["payload_json"])
    kind: OutboundKind = action["kind"]
    keys = contrib.keys()
    common: dict[str, Any] = {
        "user_directed": contrib["source"] == "user", "notes": contrib["notes"] or "" if "notes" in keys else "",
        "expires_at": expires_at,
    }  # fmt: skip
    if kind in {"comment", "issue_claim"}:
        return CardView(
            kind=kind, cid=contrib["id"], upstream=contrib["upstream"], dry_run=dry_run,
            body=payload["body"], target_number=payload["target_number"], **common,
        )  # fmt: skip
    title = contrib["pr_title_override"] if contrib["pr_title_override"] is not None else contrib["pr_title"]
    body = contrib["pr_body_override"] if contrib["pr_body_override"] is not None else contrib["pr_body"]
    rev = payload.get("rev") if isinstance(payload.get("rev"), int) else None
    plan = _json(contrib["plan_json"])
    asked: tuple[AskedItem, ...] = ()
    asked_total = 0
    info = interdiff_info(contrib["id"], rev) if kind == "open_pr" else None
    if kind == "open_pr" and rev is not None and rev >= 2:
        asked, asked_total = asked_items(conn, contrib["id"], info)
    report = _json(contrib["size_report_json"]) if "size_report_json" in keys else {}
    return CardView(
        kind=kind,
        cid=contrib["id"],
        upstream=contrib["upstream"],
        dry_run=dry_run,
        title=title,
        body=body,
        commit_message=contrib["commit_message"],
        numstat=_numstat(contrib),
        head_sha=payload["head_sha"],
        round_base=payload.get("old_head_sha") or contrib["base_sha"],
        base_ref=contrib["base_ref"],
        identity=payload.get("identity"),
        branch=payload["branch"],
        fork_owner=payload["fork_owner"],
        pr_number=payload.get("pr_number"),
        edited=contrib["pr_title_override"] is not None or contrib["pr_body_override"] is not None,
        rev=rev,
        issue_number=contrib["issue_number"],
        issue_title=contrib["issue_title"],
        issue_url=contrib["issue_url"] if (contrib["issue_url"] or "").startswith("https://github.com/") else None,
        size_report=report or None,
        validation=_json(contrib["validation_json"]),
        review=_json(contrib["review_json"]),
        models=_json(contrib["models_json"]),
        risk=contrib["risk"],
        confidence=contrib["confidence"],
        rationale=str(plan.get("summary") or plan.get("rationale") or "") or None,
        interdiff=info,
        asked=asked,
        asked_total=asked_total,
        vetting=_json(contrib["vetting_json"]) if "vetting_json" in keys else {},
        **common,
    )


def _repo_policy_block(view: CardView) -> k.Block | None:
    """``✅ AI policy: allowed with conditions (label) · checklist ⚠️ missing: label`` (§14.11.1): the
    status in fixed wording and the conditions checked by the gatekeeper on the card's own title/body
    (a human edit may remove a line; that is shown, not blocked)."""
    if view.kind != "open_pr" or not view.vetting:
        return None
    conditions = ai_policy.parse_conditions(view.vetting.get("ai_conditions"))
    text = f"AI policy: {ai_policy.display(view.vetting.get('ai_policy'), conditions)}"
    shown = [c for c in conditions if not (c.kind in ai_policy.VALUE_KINDS and not ai_policy.sanitize_value(c.value))]
    if shown:
        missing = ai_policy.missing_conditions(view.title or "", view.body or "", shown)
        kinds = {c.kind for c in shown}
        if missing:
            text += " · checklist ⚠️ missing: " + ", ".join(ai_policy.KIND_LABELS[m] for m in missing)
        else:
            text += f" · checklist ✅ {len(kinds)}/{len(kinds)}"
    line: k.Line = (k.Const("✅ "), k.Const(text))
    return k.Block((k.Lines((line,)),), required=False, priority=8, name="repo_policy")


TOUCH_WINDOW = 3  # "✅ touched" = the interdiff changes the commented lines ± 3 (new side, §14.11.7)


def touched(comment: dict[str, Any], info: dict[str, Any] | None) -> bool | None:
    if info is None or info.get("mode") == "unavailable":
        return None
    files = {str(f.get("path")): f for f in info.get("files") or [] if isinstance(f, dict)}
    path = comment.get("path")
    if not path:
        return bool(files)  # a general comment: anything changed
    entry = files.get(str(path))
    if entry is None:
        return False
    start, end = comment.get("line_start"), comment.get("line_end") or comment.get("line_start")
    if info.get("mode") != "interdiff" or not isinstance(start, int) or not isinstance(end, int):
        return True  # file-level granularity (range-diff, or a file-level comment)
    for lo, hi in entry.get("new_ranges") or []:
        if int(lo) <= end + TOUCH_WINDOW and int(hi) >= start - TOUCH_WINDOW:
            return True
    return False


def asked_items(conn: sqlite3.Connection, cid: str, info: dict[str, Any] | None) -> tuple[tuple[AskedItem, ...], int]:
    """The comments of the change request this rev answers (the newest consumed one)."""
    row = db.one(conn, "SELECT comments_json, text FROM change_requests WHERE cid = ? AND consumed_at IS NOT NULL"
                 " ORDER BY consumed_at DESC LIMIT 1", (cid,))  # fmt: skip
    if row is None:
        return (), 0
    try:
        comments = json.loads(row["comments_json"]) if row["comments_json"] else []
    except ValueError:
        comments = []
    if not comments:
        return (AskedItem("general", str(row["text"]), touched({}, info)),), 1
    items = []
    for c in comments:
        if not isinstance(c, dict):
            continue
        where = str(c.get("path") or "general")
        if c.get("path") and isinstance(c.get("line_start"), int):
            end = c.get("line_end") or c["line_start"]
            where += f" L{c['line_start']}" + (f"–{end}" if end != c["line_start"] else "")
        items.append(AskedItem(where, str(c.get("text", "")), touched(c, info), str(c.get("coder", ""))))
    return tuple(items[:YOU_ASKED_MAX]), len(items)


def view_for_approval(conn: sqlite3.Connection, approval_id: str) -> CardView:
    row = db.one(conn, "SELECT action_id, dry_run_shown, expires_at FROM approvals WHERE id = ?", (approval_id,))
    if row is None:
        raise KeyError(approval_id)
    return view_for_action(conn, row["action_id"], bool(row["dry_run_shown"]), from_iso(row["expires_at"]))


def part_count(conn: sqlite3.Connection, action_id: str) -> int:
    """``body_parts_total`` for a new approval of this action (rendering is mode-independent)."""
    return max(1, len(render(view_for_action(conn, action_id, True)).parts))


def file_count(conn: sqlite3.Connection, action_id: str) -> int:
    try:
        return len(view_for_action(conn, action_id, True).numstat)
    except KeyError:
        return 0
