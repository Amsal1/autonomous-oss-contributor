"""The terminal FYI: one P2 note for every move to FAILED or ABANDONED after gate 1 (DESIGN §14.13.1, §9.10).

``queue_terminal_fyi`` is the ``StateMachine.on_terminal`` hook ``context.py`` injects (so
``state_machine.py`` never imports Telegram). The state machine calls it only for an item that passed
gate 1, when the move is not a live-PR redirect and not the owner's own action. It reports and decides
nothing: no buttons (a row with buttons is never held or batched, §14.13.3), so no new approval path.

The card is built with the kit only. The cid is a ``k.Id`` (one tap to copy, ``ui/ids.py``); the subject,
the stored ``state_reason`` and the summary (runner evidence, often the model's own words) are ``k.Text``
spans, so both renderers redact them (``ui/redact.py``), make them visible and escape them. The summary is
a 🤖 claim (U6). It goes to 📈 OSS feed at priority 2, category ``stages``: silent, held in quiet hours,
batched. The dedupe key ``terminal:<cid>`` is once ever (BA-3): an item ends once.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ossbot.common.schemas import State
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send

SUBJECT_SHOWN = 160
REASON_SHOWN = 200
SUMMARY_SHOWN = 600


def subject(row: sqlite3.Row) -> str:
    """``owner/repo#N``, or the repo alone for a repo-level item."""
    return str(row["upstream"]) + (f"#{row['issue_number']}" if row["issue_number"] else "")


def terminal_view(cid: str, subject_: str, from_: State, to: State, reason: str | None, summary: str) -> k.View:
    """❌ Failed (⚪ Abandoned) · owner/repo#N · c_… / Stage · reason / 🤖 Why: … / where the logs are."""
    icon, title = ("⚪", "Abandoned") if to is State.ABANDONED else ("❌", "Failed")
    head: k.Line = (k.Const(f"{icon} "), k.Bold(k.Const(title)), k.Const(" · "), k.Text(subject_, SUBJECT_SHOWN),
                    k.Const(" · "), k.Id(cid))  # fmt: skip
    facts: list[k.Span] = [k.Const(f"Stage: {from_.value.lower()}")]
    if reason:
        facts += [k.Const(" · reason: "), k.Text(reason, REASON_SHOWN)]
    elements: list[k.Element] = [k.Lines((head, tuple(facts)))]
    if summary:
        elements.append(k.claim("Why:", summary, max_len=SUMMARY_SHOWN))
    elements.append(k.Lines((k.line("No further work on this item. Logs: /logs ", k.Id(cid)),)))
    return k.View(kind="fyi", blocks=[k.Block(tuple(elements))], cid=cid)


def queue_terminal_fyi(
    ctx: AppContext, row: sqlite3.Row, from_: State, to: State, reason: str | None, evidence: dict[str, Any]
) -> int | None:
    """``StateMachine.on_terminal``: queue the FYI; None when deduplicated or Telegram is unset."""
    cid = str(row["id"])
    raw = evidence.get("summary")
    summary = raw.strip() if isinstance(raw, str) else ""
    view = terminal_view(cid, subject(row), from_, to, reason, summary)
    return ui_send.deliver(ctx, view, thread="feed", priority=2, dedupe_key=f"terminal:{cid}", category="stages",
                           holdable=True)  # fmt: skip
