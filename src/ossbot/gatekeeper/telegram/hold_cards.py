"""The P1 egress-anomaly hold card and its Resume two-tap (open-discovery design §13.3, ADR-0023).

``queue_hold_card`` is the ``publish_hold`` callback ``context.py`` injects into ``ContributionHolds`` (so
``holds.py`` never imports Telegram). The card is built with the kit only; the host names come from the
squid log through sandboxd (untrusted, bounded by ``HoldRequest``) and are ``k.Text`` spans, so both
renderers redact them (``ui/redact.py``), make them visible and escape them. It goes to 🚦 Control at
priority 1, never held by quiet hours (P1 is only silenced there).

``[▶️ Resume…]`` (``hold_resume``, kit token, ``approval_id`` NULL, owner- and message-bound) edits the card
into a confirm panel; ``[▶️ Resume]`` (``hold_resume_confirm``, single-use, ``confirm_ttl_seconds``) releases
the hold: a go action, human only (I9). A tap for a hold that is no longer the open one is refused.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field

from ossbot.common.schemas import HoldView
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.holds import HoldRefused
from ossbot.gatekeeper.telegram.actions import Tap, action
from ossbot.gatekeeper.telegram.review.resolve import CID_PATTERN
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send

TOP_HOSTS_SHOWN = 5
HOST_SHOWN = 60
MIB = 1024 * 1024
GONE = "This hold is no longer open."
HOLD_PATTERN = r"^h_[0-9A-HJKMNP-TV-Z]{10}$"


class HoldArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cid: str = Field(pattern=CID_PATTERN)
    hold: str = Field(pattern=HOLD_PATTERN)


def _size(n: int) -> str:
    if n >= 1024 * MIB:
        return f"{n / (1024 * MIB):.1f} GB"
    if n >= 10 * MIB:
        return f"{n // MIB} MB"
    return f"{n / MIB:.1f} MB"


def _subject(ctx: AppContext, cid: str) -> str:
    row = db.one(ctx.conn, "SELECT upstream, issue_number FROM contributions WHERE id = ?", (cid,))
    if row is None:
        return cid
    return str(row["upstream"]) + (f"#{row['issue_number']}" if row["issue_number"] else "")


def _repo(ctx: AppContext, cid: str) -> str:
    row = db.one(ctx.conn, "SELECT upstream FROM contributions WHERE id = ?", (cid,))
    return str(row["upstream"]) if row is not None else cid


def hold_view(ctx: AppContext, hold: HoldView) -> k.View:
    """🚨 Egress anomaly · owner/repo#N · c_… · <stage> job stopped, the numbers against the limits, ≤ 5 hosts."""
    d = hold.detail
    lim = d.limits
    numbers = (f"↑ {_size(d.up_bytes)} (limit {lim.max_upload_mb} MB) · ↓ {_size(d.down_bytes)} (limit"
               f" {lim.max_download_mb} MB) · {d.hosts} hosts (limit {lim.max_hosts}) · mode {d.egress_mode}"
               + (" · strict egress" if d.egress == "strict" else ""))  # fmt: skip
    subject = _subject(ctx, hold.cid)
    head: list[k.Span] = [k.Const("🚨 "), k.Bold(k.Const("Egress anomaly")), k.Const(" · ")]
    if subject != hold.cid:
        head += [k.Text(subject, 160), k.Const(" · ")]
    head += [k.Id(hold.cid), k.Const(f" · {d.stage} job stopped")]  # the cid one tap to copy (§14.17.3)
    lines: list[k.Line] = [tuple(head), k.line(numbers)]
    hosts = d.top_hosts[:TOP_HOSTS_SHOWN]
    if hosts:
        spans: list[k.Span] = [k.Const("Top hosts: ")]
        for i, host in enumerate(hosts):
            spans += [k.Const(", ")] if i else []
            spans.append(k.Text(host, HOST_SHOWN))
        lines.append(tuple(spans))
    if d.denied:
        lines.append(k.line(f"Denied requests: {d.denied}"))
    lines.append(k.line("Only this contribution is paused."))
    arg = {"cid": hold.cid, "hold": hold.id}
    rows: k.Rows = [[k.button("▶️ Resume…", "hold_resume", arg)]]
    return k.View(kind="fyi", blocks=[k.Block((k.Lines(tuple(lines)),))], rows=rows, cid=hold.cid)


def confirm_view(ctx: AppContext, hold: HoldView) -> k.View:
    subject, repo = _subject(ctx, hold.cid), _repo(ctx, hold.cid)
    ttl = timedelta(seconds=ctx.config.policy.limits.confirm_ttl_seconds)
    lines: list[k.Line] = [
        (k.Const("▶️ "), k.Bold(k.Const("Resume ")), k.Bold(k.Text(subject, 160)), k.Bold(k.Const("?"))),
        # the repo is the contribution's validated slug (REPO_RE): a copyable command, no link breakers
        (k.Const(f"The {hold.detail.stage} job runs again with the same egress. To narrow it first: "),
         k.Code(k.Const(f"ossctl targets strict {repo}")), k.Const(".")),
    ]  # fmt: skip
    arg = {"cid": hold.cid, "hold": hold.id}
    rows: k.Rows = [[k.button("▶️ Resume", "hold_resume_confirm", arg, style="danger", single_use=True, ttl=ttl),
                     k.button("↩️ Cancel", "hold_back", arg, single_use=True, ttl=ttl)]]  # fmt: skip
    return k.View(kind="fyi", blocks=[k.Block((k.Lines(tuple(lines)),))], rows=rows, cid=hold.cid)


def queue_hold_card(ctx: AppContext, hold: HoldView) -> None:
    """``ContributionHolds.publish_hold``: the P1 card in 🚦 Control (not holdable in quiet hours)."""
    ui_send.deliver(ctx, hold_view(ctx, hold), thread="control", priority=1, holdable=False)


def _open(ctx: AppContext, a: HoldArg) -> HoldView | None:
    """The tapped hold, only while it is still the contribution's open hold."""
    found = ctx.holds.get_open(a.cid)
    return found if found is not None and found.id == a.hold else None


def _gone(tap: Tap) -> str:
    ui_send.edit(tap.app, k.plain("fyi", k.line(f"⚪ Egress hold of {tap.arg.cid} is no longer open")),
                 message_id=tap.message_id)  # fmt: skip
    return GONE


@action("hold_resume", arg=HoldArg)
async def hold_resume(tap: Tap) -> str:
    """Tap 1: the confirm panel on the card."""
    found = _open(tap.app, tap.arg)
    if found is None:
        return _gone(tap)
    ui_send.edit(tap.app, confirm_view(tap.app, found), message_id=tap.message_id)
    return ""


@action("hold_back", arg=HoldArg)
async def hold_back(tap: Tap) -> str:
    found = _open(tap.app, tap.arg)
    if found is None:
        return _gone(tap)
    ui_send.edit(tap.app, hold_view(tap.app, found), message_id=tap.message_id)
    return ""


@action("hold_resume_confirm", arg=HoldArg)
async def hold_resume_confirm(tap: Tap) -> str:
    """Tap 2: release the hold (``ContributionHolds.release``, audited) and say so."""
    ctx = tap.app
    if _open(ctx, tap.arg) is None:
        return _gone(tap)
    try:
        ctx.holds.release(tap.arg.cid, tap.actor)
    except HoldRefused:
        return _gone(tap)
    subject = _subject(ctx, tap.arg.cid)
    ui_send.edit(ctx, k.plain("fyi", (k.Const("▶️ "), k.Bold(k.Const("Resumed")), k.Const(" · "),
                                      k.Text(subject, 160), k.Const(" · the stage runs again on the next tick"))),
                 message_id=tap.message_id)  # fmt: skip
    ctx.notify(f"▶️ Egress hold released for {tap.arg.cid} by you; the stage runs again.")
    return "▶️ Resumed."
