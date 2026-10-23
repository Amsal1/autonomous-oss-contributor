"""🌐 Rerun with network (ADR-0024, design §4d/§4f): the terminal FYI's button and the /queue row ``n``.

``net_rerun`` (the button, ``approval_id`` NULL) runs the read-only pre-check: a refusal is a toast with the
§4d line, otherwise the confirm card arrives as a reply (never rich, like /fix). Only its ``🌐 Rerun now`` tap
(single use, owner-, chat- and message-bound) calls ``network_rerun.start_rerun``: the human edge
``FAILED -> PROPOSAL_APPROVED``. Gate 2 is unchanged: nothing is pushed before its own two taps (I1, I3).
All handlers are ``local`` (SQLite only). This is the only importer of ``gatekeeper.network_rerun``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ossbot.common.clock import to_iso
from ossbot.gatekeeper import db, network_rerun
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.actions import Tap, action
from ossbot.gatekeeper.telegram.review.resolve import CID_PATTERN
from ossbot.gatekeeper.telegram.terminal_fyi import subject
from ossbot.gatekeeper.telegram.ui import ids
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send

REASON_SHOWN = 40


class CidArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cid: str = Field(pattern=CID_PATTERN)


def _row(ctx: AppContext, cid: str) -> Any:
    return db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))


def refusal_text(ctx: AppContext, code: str, cid: str) -> str:
    """One plain line (no HTML) per refusal code of design §4d (C-D21 wording for ``rerun_reason``)."""
    row = _row(ctx, cid)
    limit = ctx.config.policy.limits.network_reruns_max
    if code == "not_found" or row is None:
        return "no such contribution"
    if code == "not_failed":
        return f"it isn't failed (state {row['state']})"
    if code == "rerun_reason":
        reason = str(row["state_reason"] or "unknown")[:REASON_SHOWN]
        return f"it failed for {reason}, which isn't a network or sandbox-gap failure"
    if code == "rerun_gate1":
        return "it never passed gate 1 (operator imports can't be rerun)"
    if code == "pr_number_null":
        return "it has a PR; use /fix on it"
    if code == "rerun_window":
        return "it failed more than 7 days ago; send /work with the issue link to start again"
    if code == "network_reruns":
        return (f"its {limit} rerun(s) with network are used (policy.limits.network_reruns_max in config/policy.yaml"
                " + /reload)")  # fmt: skip
    if code == "duplicate_active":
        other = db.one(ctx.conn, "SELECT id FROM contributions WHERE upstream = ? AND issue_number = ? AND id != ?"
                       " ORDER BY created_at DESC LIMIT 1", (row["upstream"], row["issue_number"], cid))  # fmt: skip
        return f"another item for the same issue is active ({other['id'] if other is not None else '?'})"
    if code == "held":
        return "it is held for an egress anomaly; release the hold first"
    if code == "strict":
        return ("its repo is egress: strict, so its sandboxes never get the web (config/targets.yaml or ossctl"
                " targets strict)")  # fmt: skip
    if code == "egress_mode":
        mode = ctx.control.egress_mode(ctx.config.egress.mode_default)
        return f"the egress mode is {mode}; rerun with network needs sandbox_open (/egress)"
    if code == "setting_off":
        return "implement network is off; set on_request or auto in /egress"
    if code == "stopped":
        return "paused or killed; /resume first"
    return code


def _not_started(ctx: AppContext, code: str, cid: str) -> str:
    return f"Not started: {refusal_text(ctx, code, cid)}."


def _note(text: str) -> k.View:
    return k.View(kind="fyi", blocks=[k.Block((k.Lines((ids.spans(text),)),))])


def confirm_view(ctx: AppContext, row: Any) -> k.View:
    """The one-tap confirm (never rich, like /fix): rerun n of max, what gets network, gate 2 still asks."""
    cid = str(row["id"])
    limit = ctx.config.policy.limits.network_reruns_max
    ttl = timedelta(seconds=ctx.config.policy.limits.confirm_ttl_seconds)
    a = ctx.config.egress.anomaly
    mode = "DRY RUN" if db.current_dry_run(ctx.conn) else "LIVE"
    lines: tuple[k.Line, ...] = (
        (k.Const("🌐 "), k.Bold(k.Const("Rerun with network?")), k.Const(" · "), k.Text(subject(row), 160),
         k.Const(" · "), k.Id(cid)),
        k.line(f"Rerun {int(row['network_reruns'] or 0) + 1} of {limit}. Fetch and implement run again; the plan is"
               " reused."),
        k.line("The coder's executor (no credentials) gets the public web through the proxy for this run, behind the"
               f" always-denied floors and the per-job limits (≤ {a.max_hosts} hosts, ↑ {a.max_upload_mb} MB,"
               f" ↓ {a.max_download_mb} MB). The Kiro key container is unchanged. Gate 2 still asks you before"
               f" anything public (now {mode})."),
    )  # fmt: skip
    rows: k.Rows = [[k.button("🌐 Rerun now", "net_rerun_confirm", {"cid": cid}, style="primary", single_use=True,
                              ttl=ttl),
                     k.button("↩️ Cancel", "net_rerun_cancel", {"cid": cid}, single_use=True, ttl=ttl)]]  # fmt: skip
    return k.View(kind="confirm_prompt", cid=cid, blocks=[k.Block((k.Lines(lines),))], rows=rows)


def started_view(ctx: AppContext, done: network_rerun.RerunStarted) -> k.View:
    info = ctx.control.lifecycle()
    when = (" · starts when the bot is up." if info is not None and info.state in {"draining", "down"}
            else " · fetch runs now.")  # fmt: skip
    lines: tuple[k.Line, ...] = (
        (k.Const("🌐 "), k.Bold(k.Const(f"Rerun {done.n}/{done.max} started")), k.Const(" · "), k.Id(done.cid),
         k.Const(when)),
        k.line("The preview arrives in ✅ Reviews; nothing is pushed before you approve it."),
    )  # fmt: skip
    return k.View(kind="fyi", cid=done.cid, blocks=[k.Block((k.Lines(lines),))])


def _retire(tap: Tap) -> None:
    """The confirm card is answered: its other button dies too (a replayed Rerun after Cancel does nothing)."""
    with db.transaction(tap.app.conn):
        tap.app.conn.execute("UPDATE ui_tokens SET used_at = ? WHERE tg_chat_id = ? AND tg_message_id = ? AND"
                             " purpose IN ('net_rerun_confirm', 'net_rerun_cancel') AND used_at IS NULL",
                             (to_iso(tap.app.clock.now()), tap.chat_id, tap.message_id))  # fmt: skip


@action("net_rerun", arg=CidArg)
async def net_rerun(tap: Tap) -> str:
    """The FYI's (or /queue's) 🌐 Rerun with network: the pre-check, then the confirm card as a reply."""
    code = network_rerun.refusal(tap.app, tap.arg.cid)
    if code is not None:
        return k.toast(_not_started(tap.app, code, tap.arg.cid))
    ui_send.deliver(tap.app, confirm_view(tap.app, _row(tap.app, tap.arg.cid)), thread_id=tap.thread_id,
                    reply_to=tap.message_id)  # fmt: skip
    return "Confirm below."


@action("net_rerun_confirm", arg=CidArg)
async def net_rerun_confirm(tap: Tap) -> str:
    """The human decision: the H-only edge in SQLite, no LLM, no GitHub write."""
    _retire(tap)
    try:
        done = network_rerun.start_rerun(tap.app, tap.arg.cid, tap.actor)
    except network_rerun.RerunRefused as exc:
        ui_send.edit(tap.app, _note(_not_started(tap.app, exc.code, tap.arg.cid)), message_id=tap.message_id)
        return "Not started."
    ui_send.edit(tap.app, started_view(tap.app, done), message_id=tap.message_id)
    return "🌐 Rerun started."


@action("net_rerun_cancel", arg=CidArg)
async def net_rerun_cancel(tap: Tap) -> str:
    _retire(tap)
    ui_send.edit(tap.app, k.plain("fyi", k.line("Not started.")), message_id=tap.message_id)
    return ""
