"""S8: the Confirm panel (DESIGN §14.11.9; mockup 03; AC 6).

The card is edited in place (U3, a classic message): its first three lines and the full PR title, then
the mode line, the
exact ``confirm_text`` of the decision service for the kind and the current mode, the informational
"Reviewed" line (from ``tg_views`` opens; it never gates), the unsent-comments warning and the expiry.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from ossbot.gatekeeper import db
from ossbot.gatekeeper.telegram.escape import b, esc, tg_time

LIVE_LINES = {
    "open_pr": "🔴 <b>LIVE — this opens a public PR</b>",
    "push_update": "🔴 <b>LIVE — this pushes to the public PR</b>",
    "comment": "🔴 <b>LIVE — this posts a public comment</b>",
    "issue_claim": "🔴 <b>LIVE — this posts a public claim</b>",
}
DRY_LINE = "🧪 <b>DRY RUN — nothing will be published</b>"


def unsent_comments(conn: sqlite3.Connection, cid: str, rev: int | None) -> int:
    if rev is None:
        return 0
    row = db.one(conn, "SELECT COUNT(*) FROM review_comments WHERE cid = ? AND rev = ? AND change_request_id IS NULL"
                 " AND discarded_at IS NULL", (cid, rev))  # fmt: skip
    return int(row[0]) if row else 0


def reviewed_line(conn: sqlite3.Connection, cid: str, rev: int | None, files: int) -> str:
    """Which views were opened for this rev (informational only)."""
    if rev is None:
        return ""
    explorer = db.one(conn, "SELECT 1 FROM tg_views WHERE cid = ? AND rev = ? AND kind = 'explorer'", (cid, rev))
    paths: set[str] = set()
    for r in conn.execute("SELECT path, state_json FROM tg_views WHERE cid = ? AND rev = ? AND kind = 'file'",
                          (cid, rev)):  # fmt: skip
        try:
            seen = json.loads(r["state_json"] or "{}").get("seen", [])
        except ValueError:
            seen = []
        paths |= {str(p) for p in seen} | ({str(r["path"])} if r["path"] else set())
    opened_files = len(paths)
    summary = "✅" if explorer else "—"
    file_mark = "✅" if files and opened_files >= files else ("⚠️" if files else "—")
    return f"Reviewed: summary {summary} · files {min(opened_files, files)}/{files} {file_mark}"


def panel(card_html: str, *, kind: str, live: bool, sentence: str, cid: str, rev: int | None, files: int,
          conn: sqlite3.Connection, now: datetime, confirm_ttl_s: int) -> str:  # fmt: skip
    card_lines = card_html.split("\n")
    head = card_lines[:3]
    # the PR title is what gets published: the panel repeats it in full (mockup 03, U14)
    head += [ln for ln in card_lines[3:] if ln.startswith("PR title: ")][:1]
    lines = ["\n".join(head), LIVE_LINES.get(kind, LIVE_LINES["open_pr"]) if live else DRY_LINE, b(sentence)]
    if kind in {"open_pr", "push_update"}:
        line = reviewed_line(conn, cid, rev, files)
        if line:
            lines.append(esc(line))
    unsent = unsent_comments(conn, cid, rev)
    if unsent:
        lines.append(f"⚠️ {unsent} unsent comment{'s' if unsent != 1 else ''} will be discarded")
    until = now + timedelta(seconds=confirm_ttl_s)
    lines.append("Expires " + tg_time(int(until.timestamp()), f"in {confirm_ttl_s // 60} minutes"))
    return "\n".join(lines)
