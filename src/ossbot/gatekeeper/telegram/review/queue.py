"""The approval queue (DESIGN §14.11.13; AC 11, AC 43; mockup 12).

Ordering is deterministic: ``(class_rank, expires_at, -target_priority, created_at)``.

* Class 0: someone is waiting (a ``comment`` on our PR).
* Class 1: ``issue_claim`` and every 👤 user-directed preview or proposal.
* Class 0 also: a ``push_update`` whose fix round a maintainer review started.
* Class 1 also: a ``push_update`` of the owner's own fix round (``/fix``), labelled ``🔧 PR #n your fix``.
* Class 2: other ``push_update`` and ceiling-hit cards; autonomous proposals within 6 h of expiry.
* Class 3: ``open_pr`` previews, RESCOPE_REQUIRED cards and skill cards.
* Class 4: autonomous proposals (CR-004 rank score, descending, instead of the target priority).
* Class 5: other ``comment``.

* Class 0 also: an open egress hold (a stage is stopped); class 1 also: a failed submission.

The queue lists what waits on the owner whether or not a live card exists (an invalidated or expired
card of a still-eligible item is listed with ``· no live card`` / ``· ⌛ expired``). ``[📥 Open n]``
re-sends with ``/approve`` semantics and the expiry kept (``AppContext.reissue`` / ``reissue_proposal`` /
``reissue_skill``; an expired window restarts, like 🔁 Retry), so opening an item never moves it; a hold
re-sends its hold card, a failed submission does what the retry note's 🔁 Retry does. Gate 2 (approval
cards) is listed before gate 1 (proposals). Every item but a skill shows its cid one tap to copy
(``k.Id``, §14.17.3).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from ossbot.common.clock import from_iso, to_iso
from ossbot.common.config import resolve_target
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import reissue_eligible
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui.limits import middle_cut

PER_PAGE = 5
LABEL_MAX = 90
PROMOTE_WITHIN = timedelta(hours=6)
RESCOPE_TTL = timedelta(hours=72)  # the rescope/ceiling card's buttons (rescope_cards.TTL)
RETRY_TTL = timedelta(hours=24)  # the retry note's 🔁 Retry token (publisher.RETRY_TOKEN_TTL)
NO_CARD_MARK = " · no live card"
EXPIRED_MARK = " · ⌛ expired"
_SAFE = re.compile(r"[^A-Za-z0-9_:-]")
Sort = Literal["priority", "oldest"]
Family = Literal["a", "p", "s", "r", "h", "f"]


@dataclass(frozen=True)
class Item:
    gate: int  # 2 = approval cards, 1 = proposals
    class_rank: int
    expires_at: datetime
    target_priority: int
    created_at: str
    # r: a rescope or ceiling-hit card (no approval yet); h: an open egress hold; f: a failed submission
    family: Family
    subject: str  # cid, the skill name, or (f) the failed action id
    label: str
    score: float = 0.0
    cid: str = ""  # shown one tap to copy after the label (§14.17.3); empty for skills

    @property
    def shown_cid(self) -> str:
        return self.cid or (self.subject if self.family in {"a", "p", "r", "h"} else "")

    def key(self, sort: Sort) -> tuple[Any, ...]:
        if sort == "oldest":
            return (-self.gate, self.expires_at, self.created_at)
        tie = -self.score if self.class_rank == 4 else -self.target_priority
        return (-self.gate, self.class_rank, self.expires_at, tie, self.created_at)


def _priority(ctx: AppContext, upstream: str) -> int:
    target = resolve_target(ctx.config.targets, upstream)
    return int(getattr(target, "priority", 0) or 0) if target is not None else 0


FixTrigger = Literal["maintainer", "owner"]
_TRIGGERS: dict[str, FixTrigger] = {"fix_round": "maintainer", "owner_fix_round": "owner"}


def fix_trigger(ctx: AppContext, cid: str) -> FixTrigger | None:
    """Who started the current fix round: the newest WATCHING -> IMPLEMENTING transition (the round
    boundary; in-round edges such as a validation retry are skipped) with reason ``fix_round`` (the
    watcher, after maintainer feedback) or ``owner_fix_round`` (the owner's /fix)."""
    for (data,) in ctx.conn.execute("SELECT data_json FROM audit_events WHERE cid = ? AND event = 'transition'"
                                    " ORDER BY seq DESC", (cid,)):  # fmt: skip
        doc = json.loads(data or "{}")
        if doc.get("from") == "WATCHING" and doc.get("to") == "IMPLEMENTING":
            return _TRIGGERS.get(str((doc.get("evidence") or {}).get("reason")))
    return None


def maintainer_triggered(ctx: AppContext, cid: str) -> bool:
    """A ``push_update`` whose fix round a maintainer review started (WATCHING -> IMPLEMENTING, fix_round)."""
    return fix_trigger(ctx, cid) == "maintainer"


def _action_rank(ctx: AppContext, r: Any) -> int:
    kind = str(r["kind"])
    if kind == "comment":
        target = json.loads(r["payload_json"]).get("target_number")
        return 0 if r["pr_number"] and target == r["pr_number"] else 5
    trigger = fix_trigger(ctx, str(r["cid"])) if kind == "push_update" else None
    if trigger == "maintainer":
        return 0
    if kind == "issue_claim" or r["source"] == "user" or trigger == "owner":
        return 1
    return 2 if kind == "push_update" else 3


def _action_label(ctx: AppContext, r: Any) -> str:
    kind = str(r["kind"])
    icon = {"open_pr": "🧩", "push_update": "🔁", "comment": "💬", "issue_claim": "🙋"}[kind]
    what = {"open_pr": "PR preview", "push_update": f"PR #{r['pr_number']} fix", "comment": "comment",
            "issue_claim": "claim"}[kind]  # fmt: skip
    if kind == "push_update" and fix_trigger(ctx, str(r["cid"])) == "owner":
        icon, what = "🔧", f"PR #{r['pr_number']} your fix"
    return f"{icon} {what} · {middle_cut(str(r['upstream']), 30)}" + (" · 👤" if r["source"] == "user" else "")


def _proposal_rank(r: Any, expires: datetime, now: datetime) -> int:
    return 1 if r["source"] == "user" else (2 if expires - now <= PROMOTE_WITHIN else 4)


def _proposal_label(r: Any, version: int) -> str:
    issue = f" #{r['issue_number']}" if r["issue_number"] else ""
    return (f"💡 Proposal v{version} · {middle_cut(str(r['upstream']), 28)}{issue} · {r['est_class'] or '?'}"
            + (" · 👤" if r["source"] == "user" else ""))  # fmt: skip


def _marker(expires: datetime, now: datetime) -> str:
    return EXPIRED_MARK if expires <= now else NO_CARD_MARK


def items(ctx: AppContext) -> list[Item]:
    """Everything waiting on the owner, live card or not (§14.11.13): open cards first, then the same
    subjects without a live card (``AWAITING_APPROVAL`` and the other re-send states, ``PROPOSED``, staged
    skills), open holds, ``RESCOPE_REQUIRED`` and ``SUBMIT_FAILED``. Deduped by ``(family, subject)``."""
    now = ctx.clock.now()
    live = to_iso(now)  # expired approvals aren't listed as open cards, even before the sweep marks them
    out: list[Item] = []
    seen: set[tuple[str, str]] = set()

    def add(item: Item, *, live_card: bool = False) -> None:
        """Every open card is its own row (siblings of one cid included); the no-card sources add a
        subject only once and never next to its live card."""
        if live_card or (item.family, item.subject) not in seen:
            seen.add((item.family, item.subject))
            out.append(item)

    held = {str(r[0]) for r in ctx.conn.execute("SELECT cid FROM contribution_holds WHERE released_at IS NULL")}
    for r in ctx.conn.execute(
        "SELECT a.id, a.expires_at, a.created_at, o.id AS action_id, o.kind, o.payload_json, c.id AS cid,"
        " c.upstream, c.source, c.pr_number FROM approvals a JOIN outbound_actions o ON o.id = a.action_id"
        " JOIN contributions c ON c.id = o.cid WHERE a.status IN ('pending','armed') AND a.expires_at > ?",
        (live,),
    ).fetchall():
        add(Item(2, _action_rank(ctx, r), from_iso(r["expires_at"]), _priority(ctx, str(r["upstream"])),
                 str(r["created_at"]), "a", str(r["cid"]), _action_label(ctx, r)), live_card=True)  # fmt: skip
    # gate 2 without a live card: an eligible action, nothing open or being submitted, not held
    for r in ctx.conn.execute(
        "SELECT o.id AS action_id, o.kind, o.payload_json, o.created_at AS action_created, c.id AS cid, c.state,"
        " c.upstream, c.source, c.pr_number, c.updated_at FROM outbound_actions o JOIN contributions c ON"
        " c.id = o.cid WHERE o.status = 'pending_approval' AND NOT EXISTS (SELECT 1 FROM approvals a"
        " JOIN outbound_actions o2 ON o2.id = a.action_id WHERE o2.cid = o.cid AND (a.status = 'confirmed'"
        " OR (a.status IN ('pending','armed') AND a.expires_at > ?))) ORDER BY o.created_at",
        (live,),
    ).fetchall():
        state = _state(str(r["state"]))
        if str(r["cid"]) in held or state is None or not reissue_eligible(r["kind"], "pending_approval", state):
            continue
        newest = db.one(ctx.conn, "SELECT expires_at, created_at FROM approvals WHERE action_id = ? ORDER BY"
                        " created_at DESC, rowid DESC LIMIT 1", (r["action_id"],))  # fmt: skip
        expires = from_iso(newest["expires_at"]) if newest else from_iso(r["updated_at"]) + RESCOPE_TTL
        add(Item(2, _action_rank(ctx, r), expires, _priority(ctx, str(r["upstream"])), str(r["action_created"]), "a",
                 str(r["cid"]), _action_label(ctx, r) + _marker(expires, now)))  # fmt: skip
    for r in ctx.conn.execute(
        "SELECT p.id, p.expires_at, p.created_at, p.version, c.id AS cid, c.upstream, c.source, c.issue_number,"
        " c.est_class, c.score FROM proposal_approvals p JOIN contributions c ON c.id = p.cid"
        " WHERE p.status IN ('pending','armed') AND p.expires_at > ?",
        (live,),
    ).fetchall():
        expires = from_iso(r["expires_at"])
        add(Item(1, _proposal_rank(r, expires, now), expires, _priority(ctx, str(r["upstream"])), str(r["created_at"]),
                 "p", str(r["cid"]), _proposal_label(r, int(r["version"])), float(r["score"] or 0.0)),
            live_card=True)  # fmt: skip
    # gate 1 without a live card: PROPOSED with an earlier card, nothing open or confirmed, not held
    for r in ctx.conn.execute(
        "SELECT c.id AS cid, c.upstream, c.source, c.issue_number, c.est_class, c.score, c.created_at AS"
        " contrib_created FROM contributions c WHERE c.state = 'PROPOSED' AND NOT EXISTS (SELECT 1 FROM"
        " proposal_approvals p WHERE p.cid = c.id AND (p.status = 'confirmed' OR (p.status IN ('pending','armed')"
        " AND p.expires_at > ?)))",
        (live,),
    ).fetchall():
        newest = db.one(ctx.conn, "SELECT version, expires_at FROM proposal_approvals WHERE cid = ? ORDER BY version"
                        " DESC, created_at DESC, rowid DESC LIMIT 1", (r["cid"],))  # fmt: skip
        if newest is None or str(r["cid"]) in held:
            continue
        expires = from_iso(newest["expires_at"])
        add(Item(1, _proposal_rank(r, expires, now), expires, _priority(ctx, str(r["upstream"])),
                 str(r["contrib_created"]), "p", str(r["cid"]),
                 _proposal_label(r, int(newest["version"])) + _marker(expires, now),
                 float(r["score"] or 0.0)))  # fmt: skip
    for r in ctx.conn.execute(
        "SELECT id, expires_at, created_at, skill, version FROM skill_approvals WHERE status IN ('pending','armed')"
        " AND expires_at > ?",
        (live,),
    ).fetchall():
        add(Item(2, 3, from_iso(r["expires_at"]), 0, str(r["created_at"]), "s", str(r["skill"]),
                 f"🧠 Skill {middle_cut(str(r['skill']), 30)} v{r['version']}"), live_card=True)  # fmt: skip
    # staged skills without a live card (the source says still staged and within the tier)
    for (skill,) in ctx.conn.execute("SELECT DISTINCT skill FROM skill_approvals ORDER BY skill").fetchall():
        if ("s", str(skill)) in seen or ctx.decisions.skills.source.reissuable(ctx.conn, str(skill)) is None:
            continue
        if db.one(ctx.conn, "SELECT 1 FROM skill_approvals WHERE skill = ? AND status = 'confirmed'", (skill,)):
            continue
        newest = db.one(ctx.conn, "SELECT expires_at, created_at, version FROM skill_approvals WHERE skill = ? ORDER"
                        " BY created_at DESC, rowid DESC LIMIT 1", (skill,))  # fmt: skip
        assert newest is not None
        expires = from_iso(newest["expires_at"])
        add(Item(2, 3, expires, 0, str(newest["created_at"]), "s", str(skill),
                 f"🧠 Skill {middle_cut(str(skill), 30)} v{newest['version']}" + _marker(expires, now)))  # fmt: skip
    # open holds: a stage is stopped until the owner resumes it (class 0)
    for r in ctx.conn.execute(
        "SELECT h.cid, h.held_at, c.upstream FROM contribution_holds h JOIN contributions c ON c.id = h.cid"
        " WHERE h.released_at IS NULL ORDER BY h.held_at"
    ).fetchall():
        add(Item(2, 0, from_iso(r["held_at"]), _priority(ctx, str(r["upstream"])), str(r["held_at"]), "h",
                 str(r["cid"]), f"⏸ Held · {middle_cut(str(r['upstream']), 30)} · egress anomaly"))  # fmt: skip
    # items paused for a size decision: ceiling-hit (class 2) and RESCOPE_REQUIRED (class 3) cards
    for r in ctx.conn.execute("SELECT id, upstream, updated_at, size_report_json FROM contributions WHERE state ="
                              " 'RESCOPE_REQUIRED'"):  # fmt: skip
        report = json.loads(r["size_report_json"] or "{}")
        ceiling = report.get("rescope_reason") == "ceiling"
        label = f"{'🧱 Ceiling' if ceiling else '📏 Re-approval'} · {middle_cut(str(r['upstream']), 30)}"
        add(Item(2, 2 if ceiling else 3, from_iso(r["updated_at"]) + RESCOPE_TTL, _priority(ctx, r["upstream"]),
                 str(r["updated_at"]), "r", str(r["id"]), label))  # fmt: skip
    # a failed submission: Open = the retry note's 🔁 Retry (class 1)
    for r in ctx.conn.execute("SELECT id, upstream, updated_at FROM contributions WHERE state = 'SUBMIT_FAILED'"):
        failed = db.one(ctx.conn, "SELECT id, error_class FROM outbound_actions WHERE cid = ? AND status = 'failed'"
                        " ORDER BY created_at DESC, rowid DESC LIMIT 1", (r["id"],))  # fmt: skip
        if failed is None:
            continue
        error = middle_cut(_SAFE.sub("", str(failed["error_class"] or "error")), 30)
        add(Item(2, 1, from_iso(r["updated_at"]) + RETRY_TTL, _priority(ctx, str(r["upstream"])),
                 str(r["updated_at"]), "f", str(failed["id"]),
                 f"⚠️ Submit failed · {middle_cut(str(r['upstream']), 30)} · {error}", cid=str(r["id"])))  # fmt: skip
    return out


def _state(value: str) -> State | None:
    try:
        return State(value)
    except ValueError:
        return None


def ordered(ctx: AppContext, sort: Sort = "priority") -> list[Item]:
    return sorted(items(ctx), key=lambda i: i.key(sort))


def view(ctx: AppContext, page: int = 1, sort: Sort = "priority") -> k.View:
    queue = ordered(ctx, sort)
    pages = max(1, -(-len(queue) // PER_PAGE))
    page = min(max(1, page), pages)
    start = (page - 1) * PER_PAGE
    shown = queue[start : start + PER_PAGE]
    lines: list[k.Line] = [(k.Const("📥 "), k.Bold(k.Const("Queue")), k.Const(f" · {len(queue)} waiting"))]
    gate2 = sum(1 for i in queue if i.gate == 2)
    current_gate = 0
    for n, item in enumerate(shown, start + 1):
        if item.gate != current_gate:
            current_gate = item.gate
            gate1 = len(queue) - gate2
            title = f"Gate 2 · approval cards ({gate2})" if item.gate == 2 else f"Gate 1 · proposals ({gate1})"
            lines.append((k.Bold(k.Const(title)),))
        when = " · held since " if item.family == "h" else " · expires "
        # the cid one tap to copy (/approve, /logs …); the label gives up its 15 characters so an item line
        # still shows ≤ 120 characters (§14.11.13)
        head: k.Line = (k.Const(f"{n}. "), k.Text(item.label, LABEL_MAX - (15 if item.shown_cid else 0)))
        if item.shown_cid:
            head += (k.Const(" · "), k.Id(item.shown_cid))
        if item.label.endswith(EXPIRED_MARK):
            lines.append(head)
            continue
        lines.append((*head, k.Const(when),
                      k.Time(int(item.expires_at.timestamp()), item.expires_at.strftime("%d %b %H:%M"))))  # fmt: skip
    if not queue:
        lines.append(k.line("Nothing is waiting for you."))
    opens = [k.button(f"📥 Open {start + i + 1}", "queue_open", {"family": it.family, "subject": it.subject})
             for i, it in enumerate(shown)]  # fmt: skip
    rows: k.Rows = [opens[i : i + 3] for i in range(0, len(opens), 3)]
    mark = {s: "✅ " if sort == s else "" for s in ("oldest", "priority")}
    rows.append([k.button(mark["oldest"] + "⬆️ Oldest first", "queue_sort", {"sort": "oldest"}),
                 k.button(mark["priority"] + "🎯 By priority", "queue_sort", {"sort": "priority"})])  # fmt: skip
    if pages > 1:
        rows.append(k.pager("queue_sort", {"sort": sort}, page, pages))
    return k.View(kind="picker", blocks=[k.Block((k.Lines(tuple(lines)),))], rows=rows[:4],
                  state={"page": page, "sort": sort})  # fmt: skip
