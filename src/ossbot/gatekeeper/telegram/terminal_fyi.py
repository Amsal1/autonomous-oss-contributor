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
SANDBOX_GAP_REASONS = frozenset({"implement_attempts", "validation_stuck"})


def subject(row: sqlite3.Row) -> str:
    """``owner/repo#N``, or the repo alone for a repo-level item."""
    return str(row["upstream"]) + (f"#{row['issue_number']}" if row["issue_number"] else "")


def sandbox_next_step(reason: str | None, evidence: dict[str, Any] | None) -> bool:
    """§14.13.1, §9.8: ``needs_network``, or a validation-loop end after the coder reported sandbox gaps
    (the runner's evidence ``sandbox_gap: true``), gets the ``Next:`` line instead of "No further work"."""
    if reason == "needs_network":
        return True
    return reason in SANDBOX_GAP_REASONS and (evidence or {}).get("sandbox_gap") is True


def terminal_view(cid: str, subject_: str, from_: State, to: State, reason: str | None, summary: str,
                  evidence: dict[str, Any] | None = None) -> k.View:  # fmt: skip
    """❌ Failed (⚪ Abandoned) · owner/repo#N · c_… / Stage · reason / 🤖 Why: … / where the logs are (or, for
    a sandbox failure, the next step)."""
    icon, title = ("⚪", "Abandoned") if to is State.ABANDONED else ("❌", "Failed")
    head: k.Line = (k.Const(f"{icon} "), k.Bold(k.Const(title)), k.Const(" · "), k.Text(subject_, SUBJECT_SHOWN),
                    k.Const(" · "), k.Id(cid))  # fmt: skip
    facts: list[k.Span] = [k.Const(f"Stage: {from_.value.lower()}")]
    if reason:
        facts += [k.Const(" · reason: "), k.Text(reason, REASON_SHOWN)]
    elements: list[k.Element] = [k.Lines((head, tuple(facts)))]
    if summary:
        elements.append(k.claim("Why:", summary, max_len=SUMMARY_SHOWN))
    if sandbox_next_step(reason, evidence):
        elements.append(k.Lines((k.line("Next: the sandbox could not build or test this repo offline. Logs: /logs ",
                                        k.Id(cid), ". Sending /work with the issue link starts it again after the"
                                        " sandbox is fixed."),)))  # fmt: skip
    else:
        elements.append(k.Lines((k.line("No further work on this item. Logs: /logs ", k.Id(cid)),)))
    return k.View(kind="fyi", blocks=[k.Block(tuple(elements))], cid=cid)


def queue_terminal_fyi(
    ctx: AppContext, row: sqlite3.Row, from_: State, to: State, reason: str | None, evidence: dict[str, Any]
) -> int | None:
    """``StateMachine.on_terminal``: queue the FYI; None when deduplicated or Telegram is unset."""
    cid = str(row["id"])
    raw = evidence.get("summary")
    summary = raw.strip() if isinstance(raw, str) else ""
    view = terminal_view(cid, subject(row), from_, to, reason, summary, evidence)
    return ui_send.deliver(ctx, view, thread="feed", priority=2, dedupe_key=dedupe_key(row), category="stages",
                           holdable=True)  # fmt: skip


def dedupe_key(row: sqlite3.Row) -> str:
    """Once per run (ADR-0024): ``terminal:<cid>`` before any rerun with network (existing outbox rows and
    goldens stay valid), ``terminal:<cid>:<n>`` after the n-th, so a rerun that ends again is reported."""
    reruns = int(row["network_reruns"] or 0)
    return f"terminal:{row['id']}" if reruns == 0 else f"terminal:{row['id']}:{reruns}"
