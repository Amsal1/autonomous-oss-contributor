"""P2 "🤖 Review-bot finding" cards (DESIGN §14.11.11; owner decision 2026-10-23 (b)).

The status card's poll (``status_card.collect``, the gatekeeper's own read, I12 spirit) turns each top-level
inline review comment by a bot (``common.github_bots.is_bot``) into one card, replying to the status card in
✅ Reviews, once per comment id. The card shows ``path:line`` and the body as untrusted ``Text`` (redacted,
escaped, ≤ 300 shown). ``display_body`` drops CodeRabbit's ``<details>`` blocks, HTML comments and the
"Prompt for AI agents" tail for display only; the stored and prompted finding is the raw body the gatekeeper
re-fetches at the confirm tap (``bot_finding_actions``), always inside ``<untrusted>``.

This module builds views only; it never imports ``owner_fix`` (the import-linter contract keeps the human
edge behind the Telegram action modules).
"""

from __future__ import annotations

import re
from typing import Any

from ossbot.gatekeeper.telegram.ui import kit as k

BODY_SHOWN = 300
BODY_KEPT = 1000  # the display body kept in the card's state (for ↩️ Back), already stripped
PATH_MAX = 300
BODY_MAX = 20_000
_DETAILS = re.compile(r"<details\b[^>]*>(?:(?!<details\b).)*?</details\s*>", re.IGNORECASE | re.DOTALL)
_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_AGENT_PROMPT = re.compile(r"^.*prompt for ai agents.*$", re.IGNORECASE | re.MULTILINE)
_BLANKS = re.compile(r"\n{3,}")


def display_body(text: str) -> str:
    """The finding for display: ``<details>`` blocks (innermost first), HTML comments and everything from
    a line naming "Prompt for AI agents" are dropped. Display only, never what the coder gets."""
    out = _COMMENT.sub("", text[:BODY_MAX])
    while True:
        cut = _DETAILS.sub("", out)
        if cut == out:
            break
        out = cut
    tail = _AGENT_PROMPT.search(out)
    if tail is not None:
        out = out[: tail.start()]
    return _BLANKS.sub("\n\n", out).strip()


def where(finding: dict[str, Any]) -> str:
    """``path:line`` (``?`` when GitHub gave no line)."""
    line = finding.get("line")
    return f"{str(finding.get('path') or '?')[:PATH_MAX]}:{line if isinstance(line, int) else '?'}"


def stored(finding: dict[str, Any]) -> dict[str, Any]:
    """The finding as the card's ``tg_views.state`` keeps it (↩️ Back re-renders from it, no GitHub read)."""
    return {"id": int(finding["id"]), "who": str(finding.get("who") or "")[:40],
            "path": str(finding.get("path") or "")[:PATH_MAX],
            "line": finding.get("line") if isinstance(finding.get("line"), int) else None,
            "body": display_body(str(finding.get("body") or ""))[:BODY_KEPT]}  # fmt: skip


def header(c: dict[str, Any], finding: dict[str, Any]) -> k.Line:
    return (k.Const("🤖 "), k.Bold(k.Const("Review-bot finding")), k.Const(f" · PR #{c['pr_number']} · @"),
            k.Text(str(finding.get("who") or "?"), 40), k.Const(" · "), k.Id(str(c["id"])))  # fmt: skip


def finding_view(c: dict[str, Any], finding: dict[str, Any]) -> k.View:
    """One finding: header, ``path:line``, the body (≤ 300, untrusted) and 🔧 Fix this / 🙈 Ignore / 🔗 Open."""
    cid = str(c["id"])
    keep = stored(finding)
    arg = {"cid": cid, "comment_id": keep["id"]}
    lines: tuple[k.Line, ...] = (header(c, keep), (k.Text(where(keep), 160),))
    note = k.line("Untrusted review text: the coder checks it against the current code before changing anything.")
    blocks = [k.Block((k.Lines(lines),)),
              k.Block((k.Quote(((k.Text(display_body(keep["body"]) or "(no text)", BODY_SHOWN),),)),), name="body"),
              k.Block((k.Lines((note,)),))]  # fmt: skip
    row = [k.button("🔧 Fix this", "pr_bot_fix", arg, single_use=True),
           k.button("🙈 Ignore", "pr_bot_ignore", arg, single_use=True)]  # fmt: skip
    url = str(c.get("pr_url") or "")
    if url.startswith("https://github.com/"):  # built by the gatekeeper, never the API's html_url
        row.append(k.url_button("🔗 Open ↗", f"{url}#discussion_r{keep['id']}"))
    return k.View(kind="fyi", cid=cid, blocks=blocks, rows=[row], state={"finding": keep}, rich=True)


def resolved_view(c: dict[str, Any], finding: dict[str, Any], status: str) -> k.View:
    """The card after 🔧 Fix this started a round or 🙈 Ignore: the finding's place and ``status``, no buttons."""
    keep = stored(finding)
    lines: tuple[k.Line, ...] = (header(c, keep), (k.Text(where(keep), 160),), k.line(status))
    return k.View(kind="fyi", cid=str(c["id"]), blocks=[k.Block((k.Lines(lines),))], state={"finding": keep},
                  rich=True)  # fmt: skip
