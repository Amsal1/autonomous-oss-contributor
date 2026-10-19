"""RESCOPE_REQUIRED and ceiling-hit cards (DESIGN §14.11.17-§14.11.18; PLAN item 46).

Both are gatekeeper facts (the measured size report and the configured ceiling) sent in ✅ Reviews as a
reply to the newest proposal card. The ceiling is shown read-only: no button raises it (U13); it changes
only in ``config/policy.yaml`` + ``/reload``. ``🧭 Explore`` opens the read-only explorer of the measured
rev (``action_id`` null: no Comment, no Show card, no Approve).

``✅ Continue at <class>`` is two taps (the second a single-use kit token) and calls
``ProposalService.continue_rescope``; ``📏 Make it smaller`` / ``✂️ Split`` send scout feedback; ``🔁 Re-check``
re-runs the size check (back to PACKAGING; the runner re-sends ``/package``); ``🗑 Drop`` asks once more.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.proposals import ProposalRefused, ProposalService
from ossbot.gatekeeper.state_machine import Actor, TransitionError
from ossbot.gatekeeper.telegram import proposal_actions
from ossbot.gatekeeper.telegram.actions import Tap, action
from ossbot.gatekeeper.telegram.review import actions as review_actions
from ossbot.gatekeeper.telegram.review.resolve import CID_PATTERN
from ossbot.gatekeeper.telegram.ui import icons
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send

Reason = Literal["ceiling", "overrun"]
TTL = timedelta(hours=72)
SPLIT_PLACEHOLDER = "Anything to add? (optional)"
GUARD = "This guard stops runaway changes. Split the work, or raise policy.diff in config/policy.yaml and /reload."


class RescopeArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cid: str = Field(pattern=CID_PATTERN)
    rev: int = Field(ge=1, le=10_000)


def _measured(report: dict[str, Any]) -> str:
    m = report.get("measured") or {}
    return (f"{m.get('files', 0)} files · +{m.get('code_added', 0):,} / −{m.get('code_deleted', 0):,} code ·"
            f" +{int(m.get('test_added', 0)) + int(m.get('test_deleted', 0)):,} test")  # fmt: skip


def _reply_target(ctx: AppContext, cid: str) -> int | None:
    row = db.one(ctx.conn, "SELECT tg_message_id FROM proposal_approvals WHERE cid = ? AND tg_message_id IS NOT NULL"
                 " ORDER BY version DESC, created_at DESC LIMIT 1", (cid,))  # fmt: skip
    return int(row["tg_message_id"]) if row else None


def view(ctx: AppContext, cid: str, rev: int, reason: Reason, report: dict[str, Any]) -> k.View:
    arg = {"cid": cid, "rev": rev}
    if reason == "ceiling":
        c = ctx.config.policy.diff
        ceiling = f"{c.max_files} files · {c.max_changed_lines:,} code · {c.max_test_lines:,} test"
        lines: list[k.Line] = [
            k.header(icons.CEILING, "Size ceiling reached", f"{cid} · paused"),
            k.line(f"Measured ✅ {_measured(report)}"),
            k.line(f"Ceiling (config only): {ceiling}"),
            k.line(GUARD),
        ]
        rows: k.Rows = [[k.button("✂️ Split (adjust plan)", "rescope_split", arg),
                         k.button("🗑 Drop", "rescope_drop", arg)],
                        [k.button("🔁 Re-check", "rescope_recheck", arg),
                         k.button("🧭 Explore", "rescope_explore", arg)]]  # fmt: skip
        return k.View(kind="ceiling", blocks=[k.Block((k.Lines(tuple(lines)),))], rows=rows, cid=cid, rev=rev)
    approved = str(report.get("approved_class") or "?")
    measured = str(report.get("measured_class") or "?")
    est = (report.get("estimate") or {}).get("code_lines")
    version = db.one(ctx.conn, "SELECT proposal_version FROM contributions WHERE id = ?", (cid,))
    v = int(version["proposal_version"]) if version and version["proposal_version"] else 1
    mode = k.mode_badge(db.current_dry_run(ctx.conn), na="local change")
    approved_est = f" (~{est} code)" if est is not None else ""
    lines = [
        k.header(icons.SIZE, "Re-approval needed", f"{cid} · v{v} → v{v + 1}", mode),
        (
            k.Const("Approved "),
            k.Bold(k.Const(approved)),
            k.Const(approved_est),
            k.Const(" · measured ✅ "),
            k.Bold(k.Const(measured)),
            k.Const(f": {_measured(report)}"),
        ),
    ]
    outside = [str(p) for p in report.get("outside_areas") or []]
    if outside:
        lines.append((k.Const(f"⚠️ {len(outside)} files outside the plan: "), k.Text(", ".join(outside[:3]), 200)))
    review = db.one(ctx.conn, "SELECT review_json FROM contributions WHERE id = ?", (cid,))
    try:
        reason_text = str(json.loads(review["review_json"] or "{}").get("size_reason") or "") if review else ""
    except ValueError:
        reason_text = ""
    if reason_text:
        lines.append((k.Const('🤖 coder: "'), k.Text(reason_text, 120), k.Const('"')))
    rows = [[k.button(f"✅ Continue at {measured}", "rescope_continue", arg, style="success"),
             k.button("📏 Make it smaller", "rescope_smaller", arg)],
            [k.button("🧭 Explore", "rescope_explore", arg), k.button("🗑 Drop", "rescope_drop", arg)]]  # fmt: skip
    return k.View(kind="rescope", blocks=[k.Block((k.Lines(tuple(lines)),))], rows=rows, cid=cid, rev=rev)


def send(ctx: AppContext, cid: str, rev: int, reason: Reason, report: dict[str, Any]) -> None:
    """Called by packaging when the item enters RESCOPE_REQUIRED (instead of a plain notify)."""
    ui_send.deliver(ctx, view(ctx, cid, rev, reason, report), thread="reviews",
                    reply_to=_reply_target(ctx, cid), priority=1)  # fmt: skip


def _open(ctx: AppContext, cid: str) -> bool:
    row = db.one(ctx.conn, "SELECT state FROM contributions WHERE id = ?", (cid,))
    return row is not None and row["state"] == State.RESCOPE_REQUIRED.value


def _done(tap: Tap, note: str) -> None:
    ui_send.edit(tap.app, k.plain("fyi", k.line(note)), message_id=tap.message_id)


GONE = "This item is no longer waiting for a size decision."


@action("rescope_continue", arg=RescopeArg)
async def continue_first(tap: Tap) -> str:
    a: RescopeArg = tap.arg
    if not _open(tap.app, a.cid):
        return GONE
    report = _report(tap.app, a.cid)
    measured, approved = report.get("measured_class", "?"), report.get("approved_class", "?")
    text = (f"Confirm: continue {a.cid} at size {measured} (was {approved}). Local work only — dry-run does not"
            " apply. Expires in 5 min.")  # fmt: skip
    ttl = timedelta(seconds=tap.app.config.policy.limits.confirm_ttl_seconds)
    arg = tap.arg.model_dump()
    confirm = k.button("🛠 Confirm continue", "rescope_continue_confirm", arg, style="primary", single_use=True,
                       ttl=ttl)  # fmt: skip
    rows: k.Rows = [[confirm, k.button("↩️ Cancel", "rescope_cancel", arg, single_use=True, ttl=ttl)]]
    ui_send.edit(tap.app, k.View(kind="rescope", blocks=[k.Block((k.Lines((k.line(text),)),))], rows=rows,
                                 cid=a.cid, rev=a.rev), message_id=tap.message_id)  # fmt: skip
    return ""


@action("rescope_continue_confirm", arg=RescopeArg)
async def continue_confirm(tap: Tap) -> str:
    a: RescopeArg = tap.arg
    if not _open(tap.app, a.cid):
        return GONE
    if not tap.app.approvals.system_running():
        return "Paused or killed: nothing was approved. /resume first."
    try:
        out = ProposalService(tap.app).continue_rescope(a.cid, tap.actor, at_measured=True)
    except (ProposalRefused, TransitionError) as exc:
        return f"Refused: {str(exc)[:150]}"
    _done(tap, f"✅ Continuing {a.cid} at the measured size · {out['state']} · the preview card follows")
    return "Continuing."


@action("rescope_cancel", arg=RescopeArg)
async def cancel(tap: Tap) -> str:
    a: RescopeArg = tap.arg
    if not _open(tap.app, a.cid):
        return GONE
    report = _report(tap.app, a.cid)
    reason: Reason = "ceiling" if _over(tap.app, report) else "overrun"
    ui_send.edit(tap.app, view(tap.app, a.cid, a.rev, reason, report), message_id=tap.message_id)
    return ""


def _over(ctx: AppContext, report: dict[str, Any]) -> str | None:
    """Why the measured diff is still above the configured ceiling (None when it fits)."""
    m = report.get("measured") or {}
    c = ctx.config.policy.diff
    code = int(m.get("code_added", 0)) + int(m.get("code_deleted", 0))
    test = int(m.get("test_added", 0)) + int(m.get("test_deleted", 0))
    files = int(m.get("files", 0))
    if files > c.max_files:
        return f"{files} > {c.max_files} files"
    if code > c.max_changed_lines:
        return f"{code:,} > {c.max_changed_lines:,} code lines"
    if test > c.max_test_lines:
        return f"{test:,} > {c.max_test_lines:,} test lines"
    return None


@action("rescope_recheck", arg=RescopeArg)
async def recheck(tap: Tap) -> str:
    """🔁 Re-check (ceiling): only the deterministic size check against the current ceiling."""
    a: RescopeArg = tap.arg
    if not _open(tap.app, a.cid):
        return GONE
    over = _over(tap.app, _report(tap.app, a.cid))
    if over is not None:
        return f"Still over the ceiling ({over})"
    try:
        ProposalService(tap.app).continue_rescope(a.cid, tap.actor, at_measured=False)
    except (ProposalRefused, TransitionError) as exc:
        return f"Refused: {str(exc)[:150]}"
    _done(tap, f"🔁 {a.cid} fits the ceiling now · the preview card follows")
    return "Within the ceiling now."


def _revise(tap: Tap, kind: Literal["smaller", "split"]) -> str:
    a: RescopeArg = tap.arg
    if not _open(tap.app, a.cid):
        return GONE
    row = db.one(tap.app.conn, "SELECT proposal_version FROM contributions WHERE id = ?", (a.cid,))
    version = int(row["proposal_version"] or 1) if row is not None else 1
    text = proposal_actions.revise(tap.app, a.cid, version, kind, "", tap.actor)
    if text.startswith("✏️"):
        ui_send.edit(tap.app, k.View(kind="rescope", blocks=[k.Block((k.Lines((k.line(f"⛔ Revising v{version} →"
                     f" v{version + 1}…"),)),))], cid=a.cid, rev=a.rev), message_id=tap.message_id)  # fmt: skip
        if kind == "split":  # the optional follow-up (a ForceReply can't be pre-filled)
            proposal_actions.prompt_adjust(tap.app, a.cid, version, None, reply_to=tap.message_id,
                                           text="✂️ Split requested.", placeholder=SPLIT_PLACEHOLDER)  # fmt: skip
    return text


@action("rescope_smaller", arg=RescopeArg)
async def smaller(tap: Tap) -> str:
    return _revise(tap, "smaller")


@action("rescope_split", arg=RescopeArg)
async def split(tap: Tap) -> str:
    return _revise(tap, "split")


@action("rescope_drop", arg=RescopeArg)
async def drop(tap: Tap) -> str:
    a: RescopeArg = tap.arg
    if not _open(tap.app, a.cid):
        return GONE
    rows: k.Rows = [[k.button("🗑 Drop it", "rescope_drop_confirm", tap.arg.model_dump(), style="danger",
                              single_use=True, ttl=timedelta(minutes=5)),
                     k.button("↩️ Cancel", "rescope_cancel", tap.arg.model_dump(), single_use=True)]]  # fmt: skip
    ui_send.edit(tap.app, k.View(kind="rescope", blocks=[k.Block((k.Lines((k.line(f"Drop {a.cid}? Nothing was"
                 " published; the work is discarded."),)),))], rows=rows, cid=a.cid, rev=a.rev),
                 message_id=tap.message_id)  # fmt: skip
    return ""


@action("rescope_drop_confirm", arg=RescopeArg)
async def drop_confirm(tap: Tap) -> str:
    a: RescopeArg = tap.arg
    if not _open(tap.app, a.cid):
        return GONE
    try:
        tap.app.state_machine.transition(a.cid, State.RESCOPE_REQUIRED, State.ABANDONED, Actor.HUMAN,
                                         {"reason": "dropped_at_rescope"}, tap.actor)  # fmt: skip
    except TransitionError as exc:
        return f"Refused: {exc.code}"
    _done(tap, f"🗑 Dropped {a.cid}")
    return "Dropped."


@action("rescope_explore", arg=RescopeArg)
async def explore(tap: Tap) -> str:
    a: RescopeArg = tap.arg
    review_actions.open_explorer(tap.app, a.cid, a.rev, None)  # read-only: action_id null
    return ""


def _report(ctx: AppContext, cid: str) -> dict[str, Any]:
    row = db.one(ctx.conn, "SELECT size_report_json FROM contributions WHERE id = ?", (cid,))
    try:
        doc = json.loads(row["size_report_json"]) if row and row["size_report_json"] else {}
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}
