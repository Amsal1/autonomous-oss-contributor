"""S10/S11: the live status card of an open PR (DESIGN §14.11.11, §14.19 item 2).

One ``tg_views(kind='status')`` message per open LIVE PR in ✅ Reviews, a reply to its card. The facts come
from the gatekeeper's read-only GitHub client (PR, reviews, check runs, comments) in a spawned
``github_read`` task, polled every ``POLL_S`` per PR (and on ``🔄 Refresh``); the card is edited in place
at most once per ``telegram.status_card.min_edit_interval_s`` (60 s) and only when its content changes.
Each poll attempt is stamped before the read, so a failed read waits ``POLL_S``, doubling per consecutive
failure up to ``POLL_MAX_S`` (1 h), and never less than GitHub's rate-limit reset. The bookkeeping lives in
the status row's ``tg_views.state_json``, which an edit merges rather than replaces (``outbox``).

* **CI line**: ``CI: ✅ n · ⏳ n · ❌ n``; with no checks yet on a repo whose vetted ``ci_gating`` is
  ``approval_required`` or ``label_or_comment``, ``⏳ CI waits for maintainer approval`` instead.
* **P1 events** (a maintainer review, a comment mentioning the owner's login, a ❌ check) are new loud
  messages replying to the status card, once per event id.
* **Merged**: the card becomes ``🎉 Merged by @x · <time>`` with only the PR link, then a summary (time to
  merge, review rounds, lines changed, merges this month, streak; ``message_effect_id`` when configured).
* **Closed**: ``🗂 Closed`` with the closer and the last maintainer comment (≤ 300, quoted, untrusted).
* ``📜 Timeline`` edits in place to the PR's events; ``🛑 Stop watching`` asks for a confirm tap, then
  ``WATCHING → ABANDONED`` (actor human, M4); the PR stays open on GitHub.
* ``🔧 Fix with notes`` (WATCHING, fixes not disabled, owner rounds left): a ForceReply prompt, then the
  one-tap confirm of an owner fix round (``telegram/commands/fix.py``; this module never imports
  ``owner_fix``).
* **Bots** (``common.github_bots.is_bot``: ``user.type == "Bot"`` or a ``[bot]`` login) are not maintainers:
  they are not counted in "Comments", their reviews don't count as approvals or change requests, and they
  never raise a P1 (they stay in the timeline). Each top-level bot inline comment becomes one P2
  ``🤖 Review-bot finding`` card (``bot_finding_cards``), once per comment id, at most
  ``FINDINGS_PER_POLL`` per refresh, with ``🔧 Fix this`` / ``🙈 Ignore`` (``bot_finding_actions``).

Every GitHub-authored string (titles, logins, comments, check names) is untrusted ``Text``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from ossbot.common import ci_facts
from ossbot.common.clock import from_iso, to_iso
from ossbot.common.github_bots import is_bot
from ossbot.common.schemas import State
from ossbot.gatekeeper import db, outcomes
from ossbot.gatekeeper.state_machine import Actor, TransitionError
from ossbot.gatekeeper.telegram import bot_finding_cards
from ossbot.gatekeeper.telegram.actions import Tap, action
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

log = logging.getLogger(__name__)
POLL_S = 300
POLL_MAX_S = 3600  # the longest wait after consecutive failed reads
# In-memory poll gate per cid: covers the window before the status row exists (first send still queued);
# the persisted ``polled_at`` / ``poll_after`` / ``poll_failures`` in the row's state survive a restart.
_NEXT_POLL: dict[str, datetime] = {}
_FAILURES: dict[str, int] = {}
CHECKS_SHOWN = 8
EVENTS_KEPT = 200
FINDINGS_PER_POLL = 5  # review-bot finding cards per refresh; the rest follow on the next poll
OPEN_STATES = (State.SUBMITTED.value, State.WATCHING.value)
OK, PENDING, FAILED = "✅", "⏳", "❌"


@dataclass
class Facts:
    state: str  # open | merged | closed
    title: str
    url: str
    number: int
    merged_by: str = ""
    closed_at: str = ""
    merged_at: str = ""
    created_at: str = ""
    changed_lines: int = 0
    checks: list[tuple[str, str]] = field(default_factory=list)  # (name, ✅|⏳|❌)
    approvals: int = 0
    changes_by: list[str] = field(default_factory=list)
    maintainer_comments: int = 0
    last_comment_at: str = ""
    last_maintainer_comment: str = ""
    events: list[dict[str, str]] = field(default_factory=list)  # {id, kind, who, text, at, p1}
    findings: list[dict[str, Any]] = field(default_factory=list)  # top-level bot inline comments


def _check_icon(run: dict[str, Any]) -> str:
    if run.get("status") != "completed":
        return PENDING
    return OK if run.get("conclusion") in {"success", "neutral", "skipped"} else FAILED


async def collect(reader: Any, c: dict[str, Any], login: str) -> Facts:
    upstream, number = str(c["upstream"]), int(c["pr_number"])
    pr = await reader.pull(upstream, number) or {}
    facts = Facts("merged" if pr.get("merged") else str(pr.get("state") or "open"), str(pr.get("title") or ""),
                  str(pr.get("html_url") or c.get("pr_url") or ""), number,
                  merged_by=str((pr.get("merged_by") or {}).get("login") or ""), closed_at=str(pr.get("closed_at")
                  or ""), merged_at=str(pr.get("merged_at") or ""), created_at=str(pr.get("created_at") or ""),
                  changed_lines=int(pr.get("additions") or 0) + int(pr.get("deletions") or 0))  # fmt: skip
    sha = str((pr.get("head") or {}).get("sha") or c.get("head_sha") or "")
    if sha:
        facts.checks = [(str(r.get("name") or "?")[:60], _check_icon(r)) for r in await reader.check_runs(upstream,
                                                                                                      sha)]  # fmt: skip
        for name, icon in facts.checks:
            if icon == FAILED:
                facts.events.append({"id": f"check:{sha[:12]}:{name}", "kind": "check", "who": "", "text": name,
                                     "at": "", "p1": "1"})  # fmt: skip
    latest: dict[str, str] = {}
    for r in await reader.reviews(upstream, number):
        who = str((r.get("user") or {}).get("login") or "")
        if not who or who.lower() == login.lower():
            continue
        state = str(r.get("state") or "")
        bot = is_bot(r.get("user"))  # a bot review stays in the timeline, never a P1, never a maintainer
        if not bot:
            latest[who] = state
        facts.events.append({"id": f"review:{r.get('id')}", "kind": "review", "who": who, "text": state.lower(),
                             "at": str(r.get("submitted_at") or ""), "p1": "" if bot else "1"})  # fmt: skip
    facts.approvals = sum(1 for s in latest.values() if s == "APPROVED")
    facts.changes_by = [w for w, s in latest.items() if s == "CHANGES_REQUESTED"]
    for cm in await reader.comments_since(upstream, number, str(c.get("created_at") or "1970-01-01T00:00:00Z")):
        who = str((cm.get("user") or {}).get("login") or "")
        if not who or who.lower() == login.lower():
            continue
        body = str(cm.get("body") or "")
        if is_bot(cm.get("user")):
            facts.events.append({"id": f"comment:{cm.get('id')}", "kind": "comment", "who": who, "text": body[:300],
                                 "at": str(cm.get("created_at") or ""), "p1": ""})  # fmt: skip
            continue
        facts.maintainer_comments += 1
        facts.last_comment_at = str(cm.get("created_at") or "")
        facts.last_maintainer_comment = body
        mention = f"@{login}".lower() in body.lower() if login else False
        facts.events.append({"id": f"comment:{cm.get('id')}", "kind": "comment", "who": who, "text": body[:300],
                             "at": facts.last_comment_at, "p1": "1" if mention else ""})  # fmt: skip
    for ic in await reader.review_comments(upstream, number):
        cid_ = ic.get("id")
        if not is_bot(ic.get("user")) or ic.get("in_reply_to_id") or not isinstance(cid_, int) or cid_ < 1:
            continue  # only top-level bot findings; maintainers' inline comments stay with the watcher
        line = ic.get("line") or ic.get("original_line")
        facts.findings.append({"id": cid_, "who": str((ic.get("user") or {}).get("login") or "")[:40],
                               "path": str(ic.get("path") or "")[:bot_finding_cards.PATH_MAX],
                               "line": line if isinstance(line, int) else None,
                               "body": str(ic.get("body") or "")[:bot_finding_cards.BODY_MAX]})  # fmt: skip
    return facts


def _time(iso: str, fallback: str = "") -> k.Span:
    try:
        at = from_iso(iso) if iso.endswith("Z") or "+" in iso else datetime.fromisoformat(iso)
    except ValueError:
        return k.Const(fallback)
    return k.Time(int(at.timestamp()), at.strftime("%Y-%m-%d %H:%M"))


def _ci_line(c: dict[str, Any], f: Facts, ok: int, pending: int, failed: int) -> k.Line:
    """No checks yet on a repo whose CI starts only after a maintainer approves it or adds a label
    (vetted ``ci_gating``): neither passing nor failing, so the card says it waits."""
    if not f.checks:
        try:
            vet = json.loads(c.get("vetting_json") or "{}")
        except ValueError:
            vet = {}
        if isinstance(vet, dict) and ci_facts.from_stored(vet).gating in ci_facts.GATED:
            return k.line(ci_facts.WAITS_FOR_APPROVAL)
    return k.line(f"CI: {OK} {ok} · {PENDING} {pending} · {FAILED} {failed}")


def fix_offered(ctx: AppContext, c: dict[str, Any]) -> bool:
    """🔧 Fix with notes is shown only where /fix would be accepted (the tap and the confirm re-check):
    WATCHING, fixes not disabled (branch_moved, M4) and owner rounds left."""
    return (c.get("state") == State.WATCHING.value and c.get("fixes_disabled_reason") is None
            and int(c.get("owner_fix_rounds") or 0) < ctx.config.policy.limits.owner_fix_rounds_max)  # fmt: skip


def view(ctx: AppContext, c: dict[str, Any], f: Facts) -> k.View:
    repo = str(c["upstream"])
    ok = sum(1 for _, i in f.checks if i == OK)
    pending = sum(1 for _, i in f.checks if i == PENDING)
    failed = sum(1 for _, i in f.checks if i == FAILED)
    if f.state == "merged":
        head: k.Line = (k.Const("🎉 "), k.Bold(k.Const("Merged")), k.Const(" by @"), k.Text(f.merged_by or "?", 40),
                        k.Const(" · "), _time(f.merged_at))  # fmt: skip
        lines: list[k.Line] = [head, (k.Bold(k.Text(f.title, 200)),)]
        return k.View(kind="status", cid=str(c["id"]), rows=[[k.url_button("🔗 Open PR ↗", f.url)]] if f.url else [],
                      blocks=[k.Block((k.Lines(tuple(lines)),))])  # fmt: skip
    if f.state == "closed":
        lines = [(k.Const("🗂 "), k.Bold(k.Const("Closed")), k.Const(f" · PR #{f.number} · "), k.Text(repo, 100)),
                 (k.Bold(k.Text(f.title, 200)),)]  # fmt: skip
        blocks = [k.Block((k.Lines(tuple(lines)),))]
        if f.last_maintainer_comment:
            blocks.append(k.Block((k.Quote(((k.Text(f.last_maintainer_comment, 300),),)),), name="last"))
        return k.View(kind="status", cid=str(c["id"]), blocks=blocks,
                      rows=[[k.url_button("🔗 Open PR ↗", f.url)]] if f.url else [])  # fmt: skip
    lines = [
        (k.Const("🚀 "), k.Bold(k.Const(f"PR #{f.number} open")), k.Const(" · "), k.Text(repo, 100), k.Const(" · "),
         k.mode_badge(False)),
        (k.Bold(k.Text(f.title, 200)),),
        _ci_line(c, f, ok, pending, failed),
        (k.Const("Reviews: "), k.Text(", ".join(f"👀 @{w} requested changes" for w in f.changes_by) or "none yet",
                                      200), k.Const(f" · {f.approvals} approvals")),
        (k.Const(f"Comments: {f.maintainer_comments} from maintainers"),
         *((k.Const(" · last "), _time(f.last_comment_at)) if f.last_comment_at else ())),
        k.line(f"Watch: round {int(c.get('watch_rounds') or 0)}/{ctx.config.policy.limits.watch_rounds_max}"),
    ]  # fmt: skip
    blocks = [k.Block((k.Lines(tuple(lines)),), name="facts")]
    if f.checks:
        shown = " · ".join(f"{i} {n}" for n, i in f.checks[:CHECKS_SHOWN]) + (" · …" if len(f.checks) > 8 else "")
        blocks.append(k.Block((k.Quote(((k.Text(shown, 900),),), expandable=True),), required=False, name="checks"))
    cid = str(c["id"])
    rows: k.Rows = [[*([k.url_button("🔗 Open PR ↗", f.url)] if f.url else []),
                     k.button("🔄 Refresh", "status_refresh", {"cid": cid})],
                    [k.button("📜 Timeline", "status_timeline", {"cid": cid}),
                     k.button("🛑 Stop watching", "stop_watch", {"cid": cid})]]  # fmt: skip
    if fix_offered(ctx, c):
        rows.append([k.button("🔧 Fix with notes", "pr_fix", {"cid": cid})])
    return k.View(kind="status", cid=cid, blocks=blocks, rows=rows, rich=True)


def _hash(v: k.View) -> str:
    return hashlib.sha256(repr((v.blocks, [[b.label for b in r] for r in v.rows])).encode()).hexdigest()[:16]


def _existing(ctx: AppContext, cid: str) -> Any:
    return db.one(ctx.conn, "SELECT * FROM tg_views WHERE kind = 'status' AND cid = ? AND stale = 0"
                            " ORDER BY message_id DESC LIMIT 1", (cid,))  # fmt: skip


def _card_message(ctx: AppContext, cid: str) -> int | None:
    row = db.one(ctx.conn, "SELECT message_id FROM tg_views WHERE kind = 'card' AND cid = ? ORDER BY message_id DESC"
                           " LIMIT 1", (cid,))  # fmt: skip
    return int(row["message_id"]) if row is not None else None


def _set_state(ctx: AppContext, message_id: int, state: dict[str, Any]) -> None:
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE tg_views SET state_json = ?, updated_at = ? WHERE message_id = ?",
                         (json.dumps(state), to_iso(ctx.clock.now()), message_id))  # fmt: skip


def _summary(ctx: AppContext, c: dict[str, Any], f: Facts) -> k.View:
    zone = ZoneInfo(ctx.config.profile.telegram.timezone)
    now = ctx.clock.now()
    month_start = now.astimezone(zone).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    merged_month = int(ctx.conn.execute("SELECT COUNT(*) FROM outcomes WHERE result = 'merged' AND finished_at >= ?",
                                        (to_iso(month_start.astimezone(now.tzinfo)),)).fetchone()[0])  # fmt: skip
    from ossbot.gatekeeper.telegram import digest  # noqa: PLC0415 - digest imports the runtime

    days = outcomes.streak(digest.live_days(ctx), now.astimezone(zone).date())
    spent = ""
    if f.created_at and f.merged_at:
        try:
            delta = datetime.fromisoformat(f.merged_at.replace("Z", "+00:00")) - datetime.fromisoformat(
                f.created_at.replace("Z", "+00:00"))  # fmt: skip
            spent = f"{delta.days} d {delta.seconds // 3600} h"
        except ValueError:
            spent = ""
    lines = (k.header("🎉", "Merged", f"PR #{f.number}"),
             k.line(f"Time to merge {spent or '—'} · review rounds {int(c.get('watch_rounds') or 0)} · "
                    f"{f.changed_lines} lines changed"),
             k.line(f"Merge #{max(1, merged_month)} this month · streak {days} day{'s' if days != 1 else ''}"),
             )  # fmt: skip
    return k.View(kind="fyi", blocks=[k.Block((k.Lines(lines),))])


async def refresh(ctx: AppContext, reader: Any, cid: str, *, force: bool = False) -> bool:
    """Read the PR and send, edit or finalize its status card. True when something was queued."""
    c = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
    if c is None or c["pr_number"] is None or reader is None:
        return False
    facts = await collect(reader, dict(c), ctx.config.profile.github_login)
    v = view(ctx, dict(c), facts)
    digest_ = _hash(v)
    existing = _existing(ctx, cid)
    now = ctx.clock.now()
    if existing is None:
        if facts.state != "open":
            return False
        ui_send.deliver(ctx, v, thread="reviews", reply_to=_card_message(ctx, cid), silent=True,
                        dedupe_key=f"status:{cid}:card")  # fmt: skip
        return True
    state = json.loads(existing["state_json"] or "{}")
    mid = int(existing["message_id"])
    queued = False
    seen = set(state.get("seen", []))
    for ev in facts.events:
        if ev["id"] in seen:
            continue
        seen.add(ev["id"])
        if ev["p1"] and facts.state == "open":
            label = {"review": "👀 Review", "comment": "💬 Mention", "check": "❌ Check failed"}[ev["kind"]]
            line: k.Line = (k.Const(f"{label} · PR #{facts.number}"), *((k.Const(" · @"), k.Text(ev["who"], 40))
                            if ev["who"] else ()), k.Const(": "), k.Text(ev["text"], 300))  # fmt: skip
            ui_send.deliver(ctx, k.plain("fyi", line), thread="reviews", reply_to=mid, priority=1,
                            dedupe_key=f"status:{cid}:{ev['id']}")  # fmt: skip
            queued = True
    if facts.state == "open":  # review-bot findings: one P2 card per inline comment id, ≤ 5 per poll
        sent = 0
        for finding in facts.findings:
            key = f"finding:{finding['id']}"
            if key in seen or sent >= FINDINGS_PER_POLL:
                continue
            ui_send.deliver(ctx, bot_finding_cards.finding_view(dict(c), finding), thread="reviews", reply_to=mid,
                            priority=2, silent=True, dedupe_key=f"botfinding:{cid}:{finding['id']}")  # fmt: skip
            seen.add(key)
            sent += 1
            queued = True
    state["seen"] = sorted(seen)[-EVENTS_KEPT:]
    state["events"] = [{k_: e[k_] for k_ in ("kind", "who", "text", "at")} for e in facts.events][-50:]
    state["polled_at"] = to_iso(now)
    last_edit = state.get("edited_at")
    interval = timedelta(seconds=ctx.config.telegram.status_card.min_edit_interval_s)
    changed = digest_ != state.get("hash")
    final = facts.state != "open"
    if changed and (force or final or last_edit is None or now - from_iso(last_edit) >= interval):
        ui_send.edit(ctx, v, message_id=mid, dedupe_key=f"status:{cid}:{digest_}:{to_iso(now)}")
        state.update({"hash": digest_, "edited_at": to_iso(now)})
        queued = True
        if facts.state == "merged" and not state.get("summary_sent"):
            effect = ctx.config.telegram.effects.merged
            out = ui_send.deliver(ctx, _summary(ctx, dict(c), facts), thread="reviews", reply_to=mid,
                                  dedupe_key=f"status:{cid}:merged")  # fmt: skip
            if effect and out is not None:
                row = db.one(ctx.conn, "SELECT payload_json FROM tg_outbox WHERE id = ?", (out,))
                if row is not None:
                    payload = {**json.loads(row["payload_json"]), "message_effect_id": effect}
                    ctx.conn.execute("UPDATE tg_outbox SET payload_json = ? WHERE id = ?", (json.dumps(payload), out))
            state["summary_sent"] = True
    _set_state(ctx, mid, state)
    return queued


def reset_poll_gate() -> None:
    """Forget the in-memory poll gate (tests; a restart starts from the persisted row state)."""
    _NEXT_POLL.clear()
    _FAILURES.clear()


def _row_state(existing: Any) -> dict[str, Any]:
    if existing is None:
        return {}
    try:
        state = json.loads(existing["state_json"] or "{}")
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


def _before(now: datetime, iso: Any, seconds: float = 0.0) -> bool:
    """``now`` is earlier than ``iso + seconds`` (an unreadable stamp never blocks a poll)."""
    if not isinstance(iso, str):
        return False
    try:
        return now < from_iso(iso) + timedelta(seconds=seconds)
    except ValueError:
        return False


def _due(cid: str, state: dict[str, Any], now: datetime) -> bool:
    gate = _NEXT_POLL.get(cid)
    if gate is not None and now < gate:
        return False
    return not (_before(now, state.get("poll_after")) or _before(now, state.get("polled_at"), POLL_S))


def _wait_s(exc: Exception, failures: int, now: datetime) -> int:
    """Back off ``POLL_S`` doubling per consecutive failure, never less than GitHub's rate-limit reset
    (``GitHubError.retry_after_s`` / ``reset_epoch``), capped at ``POLL_MAX_S``."""
    wait = float(min(POLL_S * 2 ** min(failures - 1, 16), POLL_MAX_S))
    retry = getattr(exc, "retry_after_s", None)
    if isinstance(retry, (int, float)):
        wait = max(wait, float(retry))
    reset = getattr(exc, "reset_epoch", None)
    if isinstance(reset, (int, float)):
        wait = max(wait, float(reset) - now.timestamp())
    return int(min(math.ceil(wait), POLL_MAX_S))


def _stamp(ctx: AppContext, cid: str, updates: dict[str, Any], drop: tuple[str, ...] = ()) -> None:
    """Read-modify-write the status row's poll bookkeeping (no row yet: the memory gate covers it)."""
    existing = _existing(ctx, cid)
    if existing is None:
        return
    state = _row_state(existing)
    if not updates and not any(key in state for key in drop):
        return
    for key in drop:
        state.pop(key, None)
    state.update(updates)
    _set_state(ctx, int(existing["message_id"]), state)


async def tick(ctx: AppContext, reader: Any) -> int:
    """Poll the first open LIVE PR that is due (one PR per tick). The attempt is stamped before the read,
    so a failed read (403, network) waits ``POLL_S``, doubling per consecutive failure up to ``POLL_MAX_S``
    and never less than GitHub's rate-limit reset. Polling never changes the contribution's state."""
    if reader is None:
        return 0
    marks = ",".join("?" * len(OPEN_STATES))
    rows = ctx.conn.execute(f"SELECT id FROM contributions WHERE pr_number IS NOT NULL AND state IN ({marks})"  # noqa: S608
                            " ORDER BY updated_at", OPEN_STATES).fetchall()  # fmt: skip
    now = ctx.clock.now()
    for r in rows:
        cid = str(r["id"])
        state = _row_state(_existing(ctx, cid))
        if not _due(cid, state, now):
            continue
        _NEXT_POLL[cid] = now + timedelta(seconds=POLL_S)
        _stamp(ctx, cid, {"polled_at": to_iso(now)})
        try:
            await refresh(ctx, reader, cid)
        except Exception as exc:  # noqa: BLE001 - one PR's failed read never stops the others
            stored = state.get("poll_failures")
            prior = stored if isinstance(stored, int) and not isinstance(stored, bool) and stored > 0 else 0
            failures = _FAILURES.get(cid, prior) + 1
            _FAILURES[cid] = failures
            wait = _wait_s(exc, failures, now)
            _NEXT_POLL[cid] = now + timedelta(seconds=wait)
            _stamp(ctx, cid, {"poll_after": to_iso(now + timedelta(seconds=wait)), "poll_failures": failures})
            log.warning("status card refresh failed for %s (%s); next poll in %d s", cid, type(exc).__name__, wait,
                        extra={"event": "status_card_failed", "cid": cid})  # fmt: skip
        else:
            _FAILURES.pop(cid, None)
            _stamp(ctx, cid, {}, drop=("poll_after", "poll_failures"))
        return 1
    return 0


# ------------------------------------------------------------------------------------ buttons
class CidArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cid: Annotated[str, Field(pattern=r"^c_[0-9A-HJKMNP-TV-Z]{10}$")]


@action("status_refresh", arg=CidArg, effect="io")
async def status_refresh(tap: Tap) -> str:
    done = await refresh(tap.app, getattr(tap.handlers, "reader", None), tap.arg.cid, force=True)
    return "🔄 Refreshed." if done else "🔄 No change."


@action("status_timeline", arg=CidArg)
async def status_timeline(tap: Tap) -> str:
    row = _existing(tap.app, tap.arg.cid)
    events = json.loads(row["state_json"] or "{}").get("events", []) if row is not None else []
    lines: list[k.Line] = [k.header("📜", "Timeline", tap.arg.cid)]
    for e in events[-15:]:
        who = f" @{e['who']}" if e.get("who") else ""
        lines.append((k.Const(f"{e['kind']}{who}: "), k.Text(str(e.get("text") or ""), 120)))
    if not events:
        lines.append(k.line("No events yet."))
    rows = [[k.button("🔄 Refresh", "status_refresh", {"cid": tap.arg.cid})]]
    ui_send.edit(tap.app, k.View(kind="status", cid=tap.arg.cid, blocks=[k.Block((k.Lines(tuple(lines)),))],
                                 rows=rows), message_id=tap.message_id)  # fmt: skip
    return ""


@action("stop_watch", arg=CidArg)
async def stop_watch(tap: Tap) -> str:
    """M4: the confirm tap (single-use, 24 h) is a separate message."""
    rows: k.Rows = [[k.button("🛑 Stop watching", "stop_watch_confirm", {"cid": tap.arg.cid}, style="danger",
                              single_use=True),
                     k.button("↩️ Cancel", "stop_watch_cancel", {"cid": tap.arg.cid}, single_use=True)]]  # fmt: skip
    view = k.confirm_view("Stop watching this PR? No more replies or fixes are drafted; the PR stays open on"
                          " GitHub.", rows=rows)  # fmt: skip
    ui_send.deliver(tap.app, view, thread_id=tap.thread_id, reply_to=tap.message_id)
    return ""


@action("stop_watch_confirm", arg=CidArg)
async def stop_watch_confirm(tap: Tap) -> str:
    try:
        tap.app.state_machine.transition(tap.arg.cid, State.WATCHING, State.ABANDONED, Actor.HUMAN,
                                         {"reason": "stop_watching"}, tap.actor)  # fmt: skip
    except TransitionError as exc:
        text = f"Not changed: {str(exc)[:120]}"
    else:
        text = "🛑 Stopped watching. The PR stays open on GitHub."
    ui_send.edit(tap.app, k.plain("fyi", k.line(text)), message_id=tap.message_id)
    return ""


@action("stop_watch_cancel", arg=CidArg)
async def stop_watch_cancel(tap: Tap) -> str:
    ui_send.edit(tap.app, k.plain("fyi", k.line("Still watching.")), message_id=tap.message_id)
    return ""
