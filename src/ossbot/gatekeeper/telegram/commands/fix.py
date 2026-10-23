"""Owner fix rounds on a live PR (DESIGN §9.2, §14.11.11, §14.14.1; ADR-0003 addendum 2026-10-23).

``/fix <cid|PR url> <notes>`` and the status card's ``🔧 Fix with notes`` (a ForceReply prompt, kind
``pr_fix``) both answer with one confirm card. Only its ``🔧 Start fix round`` tap (single use, owner-,
chat- and message-bound, ``approval_id`` NULL) calls ``owner_fix.start_round``: one ``change_requests``
row plus the human ``WATCHING -> IMPLEMENTING`` edge. The update then arrives as a normal ``push_update``
card that needs its own two taps; nothing is pushed before (I1, I3). The notes are the owner's own words
and ride in the confirm token's ``arg`` (never in ``callback_data``).
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ossbot.common.clock import to_iso
from ossbot.gatekeeper import db, owner_fix
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.actions import Tap, action
from ossbot.gatekeeper.telegram.commands.registry import CommandContext, Reply, command
from ossbot.gatekeeper.telegram.escape import esc
from ossbot.gatekeeper.telegram.inputs import Typed, input_handler
from ossbot.gatekeeper.telegram.review.resolve import CID_PATTERN
from ossbot.gatekeeper.telegram.ui import ids
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send
from ossbot.gatekeeper.telegram.ui.sanitize import parse_github_url

USAGE = "Usage: /fix <cid or PR URL> <what to change>. Or tap 🔧 Fix with notes on the PR's status card."
NOTES_SHOWN = 300
PLACEHOLDER = "What should change? (≤ 4000 chars)"
NOTHING_PUSHED = "The update card arrives in ✅ Reviews; nothing is pushed before you approve it."


class CidArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cid: str = Field(pattern=CID_PATTERN)


class PrFixArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cid: str = Field(pattern=CID_PATTERN)
    notes: str = Field(min_length=1, max_length=owner_fix.NOTES_MAX)


def _row(ctx: AppContext, cid: str) -> Any:
    return db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))


def refusal_text(ctx: AppContext, code: str, cid: str | None, *, by_url: bool = False, length: int = 0) -> str:
    """One plain line (no HTML) for a refused /fix, button or confirm."""
    row = _row(ctx, cid) if cid else None
    pr = f"PR #{row['pr_number']}" if row is not None and row["pr_number"] is not None else "the PR"
    limit = ctx.config.policy.limits.owner_fix_rounds_max
    if code == "not_found":
        return "no ossbot PR at that URL" if by_url else "no such contribution"
    if code == "no_pr":
        return "it has no PR yet"
    if code == "not_watching":
        state = str(row["state"]) if row is not None else "unknown"
        hint = " — use ✏️ Request changes on its card" if state == "AWAITING_APPROVAL" else ""
        return f"it isn't a watched PR (state {state}); /fix works on WATCHING{hint}"
    if code == "fixes_disabled":
        return f"the PR branch has commits not made by ossbot; fixes are disabled for {pr}"
    if code == "owner_fix_rounds":
        return (f"all {limit} owner fix rounds are used (policy.limits.owner_fix_rounds_max in config/policy.yaml"
                " + /reload)")  # fmt: skip
    if code == "stopped":
        return "paused or killed; /resume first"
    if code == "empty":
        return "write what should change"
    if code == "too_long":
        return f"the notes are {length:,} characters; the limit is {owner_fix.NOTES_MAX:,}"
    return code


def _not_started(ctx: AppContext, code: str, cid: str | None, **kw: Any) -> str:
    return f"Not started: {refusal_text(ctx, code, cid, **kw)}."


def _note(text: str) -> k.View:
    """A plain refusal note (its ids one tap to copy)."""
    return k.View(kind="fyi", blocks=[k.Block((k.Lines((ids.spans(text),)),))])


def confirm_view(ctx: AppContext, row: Any, notes: str) -> k.View:
    """The one-tap confirm (never rich, U3): the PR, round n/max, the notes escaped (≤ 300 shown)."""
    cid = str(row["id"])
    limit = ctx.config.policy.limits.owner_fix_rounds_max
    ttl = timedelta(seconds=ctx.config.policy.limits.confirm_ttl_seconds)
    lines: list[k.Line] = [
        (k.Const("🔧 "), k.Bold(k.Const(f"Fix PR #{row['pr_number']}?")), k.Const(" · "),
         k.Text(str(row["upstream"]), 100), k.Const(" · "), k.Id(cid)),
        k.line(f"Round {int(row['owner_fix_rounds'] or 0) + 1} of {limit} · watch rounds are not used"),
    ]  # fmt: skip
    tail: list[k.Line] = [
        k.line("The coder works on your notes, then validation and self-review; the update card arrives in"
               " ✅ Reviews. Nothing is pushed before you approve it."),
    ]  # fmt: skip
    rows: k.Rows = [[k.button("🔧 Start fix round", "pr_fix_confirm", {"cid": cid, "notes": notes}, style="primary",
                              single_use=True, ttl=ttl),
                     k.button("↩️ Cancel", "pr_fix_cancel", {"cid": cid}, single_use=True, ttl=ttl)]]  # fmt: skip
    blocks = [k.Block((k.Lines(tuple(lines)),)), k.Block((k.Quote(((k.Text(notes, NOTES_SHOWN),),)),), name="notes"),
              k.Block((k.Lines(tuple(tail)),))]  # fmt: skip
    return k.View(kind="confirm_prompt", cid=cid, blocks=blocks, rows=rows)


def started_view(done: owner_fix.RoundStarted) -> k.View:
    lines: tuple[k.Line, ...] = (
        (k.Const("🔧 "), k.Bold(k.Const(f"Fix round {done.round}/{done.max} started")),
         k.Const(f" · PR #{done.pr_number} · "), k.Id(done.cid)),
        k.line(NOTHING_PUSHED),
    )  # fmt: skip
    return k.View(kind="fyi", cid=done.cid, blocks=[k.Block((k.Lines(lines),))], rich=True)


def _resolve(ctx: AppContext, target: str) -> tuple[str | None, bool]:
    """(cid or None, the target was a PR URL); None, False = neither a cid nor a PR URL."""
    if re.fullmatch(CID_PATTERN, target) and target.startswith("c_"):
        return target, False
    ref = parse_github_url(target)
    if ref is None or ref.ref != "pull" or ref.number is None:
        return None, False
    return owner_fix.find_by_pr(ctx, ref.upstream, ref.number), True


def _checked(ctx: AppContext, cid: str, notes: str, *, by_url: bool = False) -> tuple[Any, str, str | None]:
    """(row, the cleaned notes, a refusal line or None); the tap re-checks everything."""
    code = owner_fix.refusal(ctx, cid)
    if code is not None:
        return None, "", _not_started(ctx, code, cid, by_url=by_url)
    try:
        cleaned = owner_fix.clean_notes(notes)
    except owner_fix.FixRefused as exc:
        return None, "", _not_started(ctx, exc.code, cid, length=len(notes.strip()))
    return _row(ctx, cid), cleaned, None


@command("fix", help="Fix your open PR with notes: /fix <cid|PR url> <notes>", safety="request", effect="io")
async def fix(ctx: CommandContext, args: list[str]) -> Reply:
    if len(args) < 2:
        return Reply(esc(USAGE))
    cid, by_url = _resolve(ctx.app, args[0])
    if cid is None:
        return Reply(esc(_not_started(ctx.app, "not_found", None, by_url=True) if by_url else USAGE))
    row, notes, refused = _checked(ctx.app, cid, " ".join(args[1:]), by_url=by_url)
    if refused is not None:
        return Reply(ids.autocode(refused))
    return Reply("", view=confirm_view(ctx.app, row, notes))


def _retire(tap: Tap) -> None:
    """The confirm card is answered: its other button dies too (a replayed Start after Cancel, or a
    Cancel after Start, does nothing)."""
    with db.transaction(tap.app.conn):
        tap.app.conn.execute("UPDATE ui_tokens SET used_at = ? WHERE tg_chat_id = ? AND tg_message_id = ? AND"
                             " purpose IN ('pr_fix_confirm', 'pr_fix_cancel') AND used_at IS NULL",
                             (to_iso(tap.app.clock.now()), tap.chat_id, tap.message_id))  # fmt: skip


@action("pr_fix_confirm", arg=PrFixArg)
async def pr_fix_confirm(tap: Tap) -> str:
    """The human decision: DB work only (change request + the human edge), no LLM, no GitHub write."""
    _retire(tap)
    try:
        done = owner_fix.start_round(tap.app, tap.arg.cid, tap.arg.notes, tap.actor)
    except owner_fix.FixRefused as exc:
        ui_send.edit(tap.app, _note(_not_started(tap.app, exc.code, tap.arg.cid)), message_id=tap.message_id)
        return "Not started."
    ui_send.edit(tap.app, started_view(done), message_id=tap.message_id)
    return "🔧 Fix round started."


@action("pr_fix_cancel", arg=CidArg)
async def pr_fix_cancel(tap: Tap) -> str:
    _retire(tap)
    ui_send.edit(tap.app, k.plain("fyi", k.line("Not started.")), message_id=tap.message_id)
    return ""


@action("pr_fix", arg=CidArg)
async def pr_fix(tap: Tap) -> str:
    """The status card's 🔧 Fix with notes: a ForceReply prompt (15 min, single consumption)."""
    code = owner_fix.refusal(tap.app, tap.arg.cid)
    if code is not None:
        return k.toast(_not_started(tap.app, code, tap.arg.cid))
    row = _row(tap.app, tap.arg.cid)
    line = (k.Const(f"🔧 Describe the fix for PR #{row['pr_number']} · "), k.Id(tap.arg.cid),
            k.Const(f" (≤ {owner_fix.NOTES_MAX:,} chars). Your words go to the coder as your"
                    " instruction."))  # fmt: skip
    view = k.plain("confirm_prompt", line)
    view.placeholder = PLACEHOLDER
    view.cid = tap.arg.cid
    view.state = {"prompt": {"kind": "pr_fix", "cid": tap.arg.cid}}
    ui_send.deliver(tap.app, view, thread="reviews", reply_to=tap.message_id)
    return "Reply with what should change."


@input_handler("pr_fix")
async def on_pr_fix(t: Typed) -> None:
    p = t.prompt
    assert p is not None
    assert p.cid is not None
    row, notes, refused = _checked(t.app, p.cid, t.text)
    if refused is not None:
        ui_send.deliver(t.app, _note(refused), thread_id=t.thread_id, reply_to=t.message_id)
        return
    ui_send.deliver(t.app, confirm_view(t.app, row, notes), thread_id=t.thread_id, reply_to=t.message_id)
