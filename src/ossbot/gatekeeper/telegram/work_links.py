"""User-directed work: ``/work <url> [notes]`` and GitHub link cards (DESIGN §14.11.19, CR-004
Amendment B; PLAN item 46).

The handler only queues (``ContributionService.create_user``, DB) and spawns the GitHub read as a tracked
``github_read`` task (10 s); the task then enqueues the ``Working on it`` reply with the title and state, or
``(details unavailable)``. A link card is built only from the parsed URL, the GitHub facts and, for a
non-forwarded message, the owner's own notes (escaped, defused, ≤ 80 shown); a forward never carries notes.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ossbot.common.ai_policy import POLICY_DOC_PATHS
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.contributions import ContributionRefused
from ossbot.gatekeeper.state_machine import WORKING, Actor, TransitionError
from ossbot.gatekeeper.telegram.actions import Tap, action
from ossbot.gatekeeper.telegram.commands.registry import CommandContext, Reply, command
from ossbot.gatekeeper.telegram.escape import esc
from ossbot.gatekeeper.telegram.inputs import Typed, view_reply
from ossbot.gatekeeper.telegram.review.resolve import CID_PATTERN
from ossbot.gatekeeper.telegram.ui import icons
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send
from ossbot.gatekeeper.telegram.ui.sanitize import GithubRef, link_notes, parse_github_url

FACTS_TIMEOUT_S = 10.0
NOTES_MAX = 1000
NOTES_SHOWN = 80
USAGE = "Usage: /work https://github.com/owner/repo/issues/123 [notes]"
ACTIVE = ("DISCOVERED", "VETTING", "VETTED", "SCOUTING")


class CidArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cid: str = Field(pattern=CID_PATTERN)


class LinkArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=400)
    notes: str = Field(default="", max_length=NOTES_MAX)


async def facts(reader: Any, ref: GithubRef) -> dict[str, str] | None:
    """Title and state from the gatekeeper's read-only client (None on any failure or timeout)."""
    if reader is None:
        return None
    try:
        async with asyncio.timeout(FACTS_TIMEOUT_S):
            if ref.ref == "issue" and ref.number:
                doc = await reader.issue(ref.upstream, ref.number)
            elif ref.ref == "pull" and ref.number:
                doc = await reader.pull(ref.upstream, ref.number)
            else:
                doc = await reader.repo(ref.upstream)
    except Exception:  # noqa: BLE001 - a read failure only means "(details unavailable)"
        return None
    if not isinstance(doc, dict):
        return None
    title = str(doc.get("title") or doc.get("description") or "")
    state = "archived" if doc.get("archived") else str(doc.get("state") or ("public" if ref.ref == "repo" else ""))
    return {"title": title[:200], "state": state[:20]}


def _slot(ctx: AppContext, cid: str) -> str:
    rows = [str(r[0]) for r in ctx.conn.execute(
        f"SELECT id FROM contributions WHERE source = 'user' AND state IN ({','.join('?' * len(ACTIVE))})"  # noqa: S608
        " ORDER BY created_at, rowid", ACTIVE)]  # fmt: skip
    cap = ctx.config.policy.proposal.max_concurrent_scouts
    pos = rows.index(cid) + 1 if cid in rows else 1
    return f"Scouting now (slot {pos} of {cap})" if pos <= cap else f"Queued ({pos - cap} ahead)"


def _facts_line(ref: GithubRef, info: dict[str, str] | None) -> k.Line:
    if info is None:
        return k.line("(details unavailable)")
    parts: list[k.Span] = []
    if info["title"]:
        parts += [k.Const('"'), k.Text(info["title"], 200), k.Const('" · ')]
    return (*parts, k.Const(ref.ref + (f" · {info['state']}" if info["state"] else "")))


def working_view(ctx: AppContext, cid: str, ref: GithubRef, info: dict[str, str] | None) -> k.View:
    lines: list[k.Line] = [
        (
            k.Const(f"{icons.WORK} "),
            k.Bold(k.Const("Working on it")),
            k.Const(" · "),
            k.Text(ref.label, 150),
            k.Const(f" · {icons.USER} requested by you"),
        ),
        _facts_line(ref, info),
        k.line(f"{_slot(ctx, cid)} · the proposal card will arrive in ✅ Reviews"),
        k.line("Size preference and filters don't apply; safety checks do."),
    ]
    rows: k.Rows = [[k.button("✖️ Cancel request", "work_cancel", {"cid": cid}, single_use=True)]]
    return k.View(kind="work_request", blocks=[k.Block((k.Lines(tuple(lines)),))], rows=rows, cid=cid)


def request(ctx: AppContext, url: str, notes: str, actor: str) -> tuple[str | None, str]:
    """(cid, error text): queue the request (DB only)."""
    try:
        created = ctx.contributions.create_user(url, notes[:NOTES_MAX], actor)
    except ContributionRefused as exc:
        why = {"not_a_github_url": "that isn't a GitHub issue, PR or repo URL",
               "not_a_target": "that repo isn't in targets.yaml", "target_disabled": "that repo is disabled",
               "duplicate_active": "that issue is already being worked on"}.get(exc.code, exc.code)  # fmt: skip
        return None, f"Not queued: {why}."
    return str(created["cid"]), ""


async def send_working(ctx: AppContext, reader: Any, cid: str, ref: GithubRef, *, thread_id: int | None,
                       reply_to: int | None, edit: int | None = None) -> None:  # fmt: skip
    """The spawned ``github_read`` task: one read, then the reply (or the link card edited in place)."""
    view = working_view(ctx, cid, ref, await facts(reader, ref))
    if edit is not None:
        ui_send.edit(ctx, view, message_id=edit)
    else:
        ui_send.deliver(ctx, view, thread_id=thread_id, reply_to=reply_to)


@command("work", help="Work on a GitHub issue, PR or repo you choose (👤; filters don't apply)", safety="request")
async def work(ctx: CommandContext, args: list[str]) -> Reply:
    if not args:
        return Reply(esc(USAGE))
    ref = parse_github_url(args[0])
    if ref is None:
        return Reply(esc(f"Not queued: that isn't a GitHub issue, PR or repo URL. {USAGE}"))
    cid, error = request(ctx.app, args[0], " ".join(args[1:]), ctx.actor)
    if cid is None:
        return Reply(esc(error))
    if ctx.handlers is None:
        return Reply("", view=working_view(ctx.app, cid, ref, None))
    ctx.handlers.spawn(send_working(ctx.app, ctx.handlers.reader, cid, ref, thread_id=ctx.thread_id,
                                    reply_to=ctx.message_id), "github_read")  # fmt: skip
    return Reply("", deferred=True)


@action("work_cancel", arg=CidArg)
async def cancel(tap: Tap) -> str:
    """✖️ Cancel request: valid until the proposal card is sent."""
    ctx = tap.app
    cid = tap.arg.cid
    if db.one(ctx.conn, "SELECT 1 FROM proposal_approvals WHERE cid = ?", (cid,)):
        return "The proposal card is already out: tap ⏭ Skip on it."
    row = db.one(ctx.conn, "SELECT state FROM contributions WHERE id = ?", (cid,))
    if row is None:
        return "Unknown request."
    state = State(row["state"])
    try:
        if state in WORKING or state is State.SCOUTING:
            ctx.state_machine.transition(cid, state, State.FAILED, Actor.GATEKEEPER,
                                         {"error_class": "cancelled_by_owner"}, tap.actor)  # fmt: skip
        elif state is State.VETTED:
            ctx.state_machine.transition(cid, state, State.SKIPPED, Actor.GATEKEEPER,
                                         {"reason": "cancelled_by_owner"}, tap.actor)  # fmt: skip
        else:
            return f"Can't cancel in state {state.value}; it stops at the proposal card (⏭ Skip)."
    except TransitionError as exc:
        return f"Can't cancel now ({exc.code}); it stops at the proposal card (⏭ Skip)."
    ui_send.edit(ctx, k.plain("fyi", k.line(f"✖️ Request cancelled · {cid}")), message_id=tap.message_id)
    return "Cancelled."


# ------------------------------------------------------------------------------ refusal card
POLICY_PATHS = POLICY_DOC_PATHS  # the repo docs vet reads (common.ai_policy); a maintainer comment gets no link


def refusal_view(upstream: str, evidence: dict[str, Any]) -> k.View:
    """``🚫 Can't work on this``: the repo bans AI-assisted contributions (a 👤 request, AC 40). The quoted
    policy line is the repo's text (untrusted, ≤ 300); the link points at the policy file on GitHub."""
    lines: list[k.Line] = [(k.Const("🚫 "), k.Bold(k.Const("Can't work on this")), k.Const(": "),
                            k.Text(upstream, 150), k.Const(" bans AI-assisted contributions"))]  # fmt: skip
    quote = str(evidence.get("policy_line") or "")
    elements: list[k.Element] = [k.Lines(tuple(lines))]
    if quote:
        elements.append(k.claim("Policy", quote, max_len=300))
    rows: k.Rows = []
    path = str(evidence.get("policy_path") or "")
    if path in POLICY_PATHS:
        rows = [[k.url_button("📄 Open policy ↗", f"https://github.com/{upstream}/blob/HEAD/{path}")]]
    return k.View(kind="fyi", blocks=[k.Block(tuple(elements))], rows=rows)


def send_refusal(ctx: AppContext, cid: str, evidence: dict[str, Any]) -> None:
    """Called by the worker transition route on ``VETTING -> SKIPPED (ai_policy_banned)`` of a 👤 item."""
    row = db.one(ctx.conn, "SELECT upstream, source FROM contributions WHERE id = ?", (cid,))
    if row is None or row["source"] != "user":
        return
    ui_send.deliver(ctx, refusal_view(str(row["upstream"]), evidence), thread="reviews", priority=1)


# ------------------------------------------------------------------------------ link cards
def link_view(ref: GithubRef, info: dict[str, str] | None, notes: str) -> k.View:
    head: list[k.Span] = [k.Const(f"{icons.LINK} "), k.Bold(k.Text(ref.upstream + (f" #{ref.number}" if ref.number
                                                                                   else ""), 150)),
                          k.Const(f" · {ref.ref}")]  # fmt: skip
    if info is not None:
        if info["state"]:
            head.append(k.Const(f" · {info['state']}"))
        if info["title"]:
            head += [k.Const(' · "'), k.Text(info["title"], 200), k.Const('"')]
    lines: list[k.Line] = [tuple(head)]
    if notes:
        lines.append((k.Const('Notes: "'), k.Text(notes, NOTES_SHOWN), k.Const('"')))
    rows: k.Rows = [[k.button("🛠 Work on this", "link_work", {"url": ref.url, "notes": notes}, single_use=True),
                     k.button("✖️ Dismiss", "link_dismiss", {"url": ref.url}, single_use=True)]]  # fmt: skip
    return k.View(kind="link_card", blocks=[k.Block((k.Lines(tuple(lines)),))], rows=rows)


def notes_for(message: Any) -> str:
    """BA-8: ``""`` for a forward (``forward_origin`` set); else the text (or caption) minus the URLs,
    made visible, <= 1,000 characters. The card shows <= 80 of them, escaped and defused."""
    text = str(getattr(message, "text", None) or getattr(message, "caption", None) or "")
    return link_notes(text, forwarded=getattr(message, "forward_origin", None) is not None)


async def send_links(ctx: AppContext, reader: Any, refs: tuple[GithubRef, ...], notes: str, *,
                     thread_id: int | None, reply_to: int, more: int = 0) -> None:  # fmt: skip
    for ref in refs:
        ui_send.deliver(ctx, link_view(ref, await facts(reader, ref), notes), thread_id=thread_id, reply_to=reply_to)
    if more > 0:
        ui_send.deliver(ctx, k.plain("fyi", k.line(f"+{more} more links: use /work for each")), thread_id=thread_id,
                        reply_to=reply_to)  # fmt: skip


@view_reply("links")
async def on_links(t: Typed) -> None:
    """Routing rule 6: one link card per URL (≤ 3), facts read in a ``github_read`` task."""
    route = t.route
    if route is None or not route.links:
        return
    more = int(route.extra.get("more", "0"))
    t.handlers.spawn(send_links(t.app, t.handlers.reader, tuple(route.links), route.notes, thread_id=t.thread_id,
                                reply_to=t.message_id, more=more), "github_read")  # fmt: skip


@action("link_work", arg=LinkArg)
async def link_work(tap: Tap) -> str:
    """``🛠 Work on this`` is exactly ``/work <url> <notes>``: the card becomes ``Working on it``."""
    ref = parse_github_url(tap.arg.url)
    if ref is None:
        return "Not a GitHub link."
    cid, error = request(tap.app, tap.arg.url, tap.arg.notes, tap.actor)
    if cid is None:
        return error
    tap.handlers.spawn(send_working(tap.app, tap.handlers.reader, cid, ref, thread_id=tap.thread_id, reply_to=None,
                                    edit=tap.message_id), "github_read")  # fmt: skip
    return "🛠 Queued."


@action("link_dismiss", arg=LinkArg)
async def link_dismiss(tap: Tap) -> str:
    ui_send.edit(tap.app, k.plain("fyi", k.line("✖️ Dismissed")), message_id=tap.message_id)
    return ""
