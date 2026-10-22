"""Discovery visibility (discovery fix 5b and 5d, DESIGN §14.13.1; ADR-0003 addendum 2026-10-22).

* ``record``: a runner cycle report (``POST /v1/discovery-cycles``, ``DiscoveryCycleReport``) becomes the
  audit event ``discovery_cycle``; a finished cycle also queues one silent P2 FYI in 📈 OSS feed
  (category ``discovery``, held in quiet hours, batched): ``🔎 Discovery · N candidates · created K``, one
  line per created item (the cid as a ``k.Id``, the subject from the gatekeeper's own ``contributions``
  row) and the top drop reasons (``k.Text``: redacted, made visible, escaped).
* ``watch``: a step of the Telegram background reads. When no full cycle has finished for
  ``STALL_AFTER`` (12 h; counted from the gatekeeper's start when none ever did) it sends one P1 warning
  per episode, with the cuts since by reason. Silent while killed, paused, draining or down.

Both are read-only FYIs: no buttons, so no new approval or control path; they report and decide nothing.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from ossbot.common.clock import from_iso, to_iso
from ossbot.common.schemas import DiscoveryCycleReport
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.terminal_fyi import subject
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send

AUDIT_EVENT = "discovery_cycle"
STALL_AFTER = timedelta(hours=12)
CHECK_EVERY = timedelta(minutes=5)  # the stall check's own pace (it runs on every background pass)
SUBJECT_SHOWN = 160
REASON_SHOWN = 64
TOP_DROPS = 3
CUT_LABELS = {"search_limit": "search limit", "core_limit": "core limit", "github_unavailable": "GitHub down",
              "stopped": "stopped", "gatekeeper": "gatekeeper"}  # fmt: skip
STATE_KEY = "_discovery_watch"


# ------------------------------------------------------------------------------ the cycle FYI
def cycle_view(report: DiscoveryCycleReport, rows: list[sqlite3.Row]) -> k.View:
    """``🔎 Discovery · N candidates · created K`` / created items / what was read / ``Top drops: …``."""
    found = report.candidates + report.open_candidates
    head: list[k.Span] = [k.Const("🔎 "), k.Bold(k.Const("Discovery")),
                          k.Const(f" · {found} candidates · created {report.created_total}")]  # fmt: skip
    if report.scope == "repos":
        head.append(k.Const(" (ossctl discover)"))
    lines: list[k.Line] = [tuple(head)]
    for row in rows:
        lines.append((k.Const("• "), k.Id(str(row["id"])), k.Const(" · "), k.Text(subject(row), SUBJECT_SHOWN)))
    if report.created_total == 0:
        lines.append(k.line("No new items this cycle"))
    read = (f"Read: {report.targets_read} targets · {report.kept_read} kept · {report.open_repos_scanned} open"
            f" repos · {report.linked_pr} with a linked PR · {report.refused} refused")  # fmt: skip
    lines.append(k.line(read))
    drops = Counter(report.drops).most_common(TOP_DROPS)
    if drops:
        spans: list[k.Span] = [k.Const("Top drops: ")]
        for i, (reason, n) in enumerate(drops):
            spans += [*([k.Const(" · ")] if i else []), k.Text(reason, REASON_SHOWN), k.Const(f" ×{n}")]
        lines.append(tuple(spans))
    return k.View(kind="fyi", blocks=[k.Block((k.Lines(tuple(lines)),))])


def _rows(ctx: AppContext, cids: list[str]) -> list[sqlite3.Row]:
    """The created items as the gatekeeper knows them (a cid it has no row for is not shown)."""
    out: list[sqlite3.Row] = []
    for cid in cids:
        row = db.one(ctx.conn, "SELECT id, upstream, issue_number FROM contributions WHERE id = ?", (cid,))
        if row is not None:
            out.append(row)
    return out


def record(ctx: AppContext, report: DiscoveryCycleReport, actor: str) -> int | None:
    """Audit the report; queue the FYI for a finished cycle. Returns the outbox row id (None: none queued)."""
    with db.transaction(ctx.conn):
        head = ctx.audit.append(actor, AUDIT_EVENT, report.model_dump())
    if report.outcome != "finished":
        return None
    view = cycle_view(report, _rows(ctx, report.created))
    return ui_send.deliver(ctx, view, thread="feed", priority=2, silent=True, dedupe_key=f"discovery_cycle:{head.seq}",
                           category="discovery", holdable=True)  # fmt: skip


# ------------------------------------------------------------------------------ the stall warning
def _last_finished(ctx: AppContext) -> str | None:
    row = db.one(ctx.conn, "SELECT ts FROM audit_events WHERE event = ? AND json_extract(data_json, '$.outcome') ="
                           " 'finished' AND json_extract(data_json, '$.scope') = 'full' ORDER BY seq DESC LIMIT 1",
                 (AUDIT_EVENT,))  # fmt: skip
    return str(row["ts"]) if row is not None else None


def _cuts_since(ctx: AppContext, since: str) -> Counter[str]:
    cuts: Counter[str] = Counter()
    for (data,) in ctx.conn.execute("SELECT data_json FROM audit_events WHERE event = ? AND ts >= ?",
                                    (AUDIT_EVENT, since)):  # fmt: skip
        doc: Any = json.loads(data)
        if isinstance(doc, dict) and doc.get("outcome") == "cut":
            cuts[str(doc.get("cut_reason"))] += 1
    return cuts


def stalled_view(last: datetime | None, cuts: Counter[str]) -> k.View:
    head: k.Line = (k.Const("⚠️ "), k.Bold(k.Const("Discovery: no cycle has finished in 12 h")))
    when: k.Line = ((k.Const("Last finished: "), k.Time(int(last.timestamp()), last.strftime("%Y-%m-%d %H:%M UTC")))
                    if last is not None else k.line("Last finished: none since the gatekeeper started"))  # fmt: skip
    if cuts:
        parts = " · ".join(f"{CUT_LABELS.get(r, r)} ×{n}" for r, n in cuts.most_common())
        cut_line = k.line(f"Cut since: {parts}")
    else:
        cut_line = k.line("No cut cycle was reported since either")
    tail = k.line("The runner keeps retrying. Details: ossctl status, journalctl -u ossbot-runner.")
    return k.View(kind="fyi", blocks=[k.Block((k.Lines((head, when, cut_line, tail)),))])


async def watch(ctx: AppContext, reader: Any = None) -> int | None:
    """One P1 per episode when no full cycle finished for 12 h; nothing while not running and up."""
    now = ctx.clock.now()
    state: dict[str, Any] = ctx.__dict__.setdefault(STATE_KEY, {"start": now, "next": None})
    view = ctx.control.view()
    if view.state != "running" or view.lifecycle != "up":
        return None
    if state["next"] is not None and now < state["next"]:
        return None
    state["next"] = now + CHECK_EVERY
    last = _last_finished(ctx)
    baseline = from_iso(last) if last is not None else state["start"]
    if now - baseline < STALL_AFTER:
        return None
    key = f"discovery_stalled:{last}" if last is not None else f"discovery_stalled:start:{to_iso(state['start'])}"
    msg = stalled_view(from_iso(last) if last is not None else None, _cuts_since(ctx, to_iso(baseline)))
    return ui_send.deliver(ctx, msg, thread="feed", priority=1, dedupe_key=key, category="discovery")
