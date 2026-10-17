"""Minimal approval cards (PLAN Decision 7; the decorated §14.4 renderer replaces ``render`` in FEAT-005).

Rendered only from gatekeeper-derived data in the DB (derived numstat, derived commit message,
stored effective title/body). Message 1 is the card; messages 2..K+1 carry the bytes that will
be published verbatim in escaped ``<pre>`` blocks (``body_parts_total`` = number of those parts).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ossbot.common.gitcmd import NumstatEntry
from ossbot.common.schemas import OutboundKind
from ossbot.gatekeeper import db
from ossbot.gatekeeper.telegram.escape import b, code, esc, make_visible

MESSAGE_MAX = 3900  # escaped characters per message (Bot API limit 4096)
PART_MAX = 3500  # escaped characters of one <pre> chunk
NUMSTAT_LINES = 15


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


@dataclass(frozen=True)
class CardMessages:
    card: str
    parts: list[str] = field(default_factory=list)


def _cost(ch: str) -> int:
    return len(esc(make_visible(ch)[0]))  # both passes are per character, so cost is additive


def _chunks(text: str, limit: int = PART_MAX) -> list[str]:
    """Split ``text`` so every chunk's escaped, visible form is <= ``limit``; line boundaries
    first, a hard split inside a line only when the line alone is too long."""
    out: list[str] = []
    cur: list[str] = []
    cur_cost = 0

    def flush() -> None:
        nonlocal cur, cur_cost
        if cur:
            out.append("".join(cur))
        cur, cur_cost = [], 0

    for line in text.splitlines(keepends=True):
        line_cost = sum(_cost(ch) for ch in line)
        if line_cost <= limit:
            if cur_cost + line_cost > limit:
                flush()
            cur.append(line)
            cur_cost += line_cost
            continue
        for ch in line:
            c = _cost(ch)
            if cur_cost + c > limit:
                flush()
            cur.append(ch)
            cur_cost += c
    flush()
    return out or [""]


def verbatim_parts(label: str, text: str) -> list[str]:
    chunks = _chunks(text)
    total = len(chunks)
    return [f"{b(f'{label} ({i}/{total})')}\n<pre>{esc(make_visible(c)[0])}</pre>" for i, c in enumerate(chunks, 1)]


def _mode(dry_run: bool) -> str:
    return "<b>DRY RUN</b>" if dry_run else "<b>LIVE</b>"


def _numstat_lines(numstat: tuple[NumstatEntry, ...]) -> list[str]:
    added = sum(e.added or 0 for e in numstat)
    deleted = sum(e.deleted or 0 for e in numstat)
    lines = [f"Change (derived by gatekeeper): {len(numstat)} files, +{added} −{deleted}"]
    for entry in numstat[:NUMSTAT_LINES]:
        stat = "binary" if entry.added is None else f"+{entry.added} −{entry.deleted}"
        lines.append(f"  {code(entry.path)} {stat}")
    if len(numstat) > NUMSTAT_LINES:
        lines.append(f"  … and {len(numstat) - NUMSTAT_LINES} more")
    return lines


def render(view: CardView) -> CardMessages:
    short = (view.head_sha or "")[:7]
    base = (view.round_base or "")[:7]
    lines: list[str]
    parts: list[str] = []
    if view.kind in {"open_pr", "push_update"}:
        if view.kind == "open_pr":
            lines = [f"🧩 {b('Ready for review')} · {esc(view.cid)} · {_mode(view.dry_run)}", b(view.upstream)]
            lines.append(f"PR title: {code(view.title or '')}" + ("   [edited by you]" if view.edited else ""))
        else:
            lines = [f"🔁 {b(f'Fix ready for PR #{view.pr_number}')} · {esc(view.cid)} · {_mode(view.dry_run)}"]
            lines.append(f"{b(view.upstream)} · incremental commit on {code(f'{view.fork_owner}:{view.branch}')}")
        lines += _numstat_lines(view.numstat)
        on = f"{view.base_ref}@{base}" if view.kind == "open_pr" else f"{base} (current PR head)"
        lines.append(
            f"Commit {code(short)} = 1 commit on {esc(on)} · author/committer/sign-off: {esc(view.identity or '')}"
        )
        lines.append(
            f"Branch {code(view.branch or '')} → {esc(f'{view.fork_owner}/' + view.upstream.split('/', 1)[1])}"
        )
        if view.kind == "open_pr":
            body = view.body or ""
            body_parts = verbatim_parts("PR body", body)
            parts += body_parts
            lines.append(f"Body: {len(body)} chars, {len(body_parts)} part(s) below · Commit message below")
        parts += verbatim_parts("Commit message", view.commit_message or "")
    else:
        what = "Comment ready" if view.kind == "comment" else "Issue claim ready"
        icon = "💬" if view.kind == "comment" else "🙋"
        body = view.body or ""
        body_parts = verbatim_parts("Comment", body)
        parts += body_parts
        lines = [
            f"{icon} {b(what)} · {esc(view.cid)} · {_mode(view.dry_run)}",
            f"Target: {b(f'{view.upstream}#{view.target_number}')}",
            f"Body: {len(body)} chars, {len(body_parts)} part(s) below",
        ]
    _, hidden = make_visible((view.body or "") + (view.commit_message or "") + (view.title or ""))
    if hidden:
        lines.append(f"⚠️ {hidden} invisible/bidi/control character(s) shown as ⟦U+XXXX⟧")
    card = "\n".join(lines)
    if len(card) > MESSAGE_MAX:
        card = card[: MESSAGE_MAX - 20].rsplit("\n", 1)[0] + "\n…"
    return CardMessages(card=card, parts=parts)


# ----------------------------------------------------------------------------- DB views
def _numstat(contrib: sqlite3.Row) -> tuple[NumstatEntry, ...]:
    raw: list[dict[str, Any]] = json.loads(contrib["diffstat_json"] or "[]")
    return tuple(NumstatEntry(d.get("added"), d.get("deleted"), str(d.get("path", ""))) for d in raw)


def view_for_action(conn: sqlite3.Connection, action_id: str, dry_run: bool) -> CardView:
    action = db.one(conn, "SELECT * FROM outbound_actions WHERE id = ?", (action_id,))
    if action is None:
        raise KeyError(action_id)
    contrib = db.one(conn, "SELECT * FROM contributions WHERE id = ?", (action["cid"],))
    if contrib is None:
        raise KeyError(action["cid"])
    payload: dict[str, Any] = json.loads(action["payload_json"])
    kind: OutboundKind = action["kind"]
    if kind in {"comment", "issue_claim"}:
        return CardView(
            kind=kind, cid=contrib["id"], upstream=contrib["upstream"], dry_run=dry_run,
            body=payload["body"], target_number=payload["target_number"],
        )  # fmt: skip
    title = contrib["pr_title_override"] if contrib["pr_title_override"] is not None else contrib["pr_title"]
    body = contrib["pr_body_override"] if contrib["pr_body_override"] is not None else contrib["pr_body"]
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
    )


def view_for_approval(conn: sqlite3.Connection, approval_id: str) -> CardView:
    row = db.one(conn, "SELECT action_id, dry_run_shown FROM approvals WHERE id = ?", (approval_id,))
    if row is None:
        raise KeyError(approval_id)
    return view_for_action(conn, row["action_id"], bool(row["dry_run_shown"]))


def part_count(conn: sqlite3.Connection, action_id: str) -> int:
    """``body_parts_total`` for a new approval of this action (rendering is mode-independent)."""
    return max(1, len(render(view_for_action(conn, action_id, True)).parts))
