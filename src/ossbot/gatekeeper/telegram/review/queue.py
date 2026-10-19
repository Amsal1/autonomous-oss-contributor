"""The approval queue (DESIGN §14.11.13; AC 11, AC 43; mockup 12).

Ordering is deterministic: ``(class_rank, expires_at, -target_priority, created_at)``.

* Class 0: someone is waiting (a ``comment`` on our PR).
* Class 1: ``issue_claim`` and every 👤 user-directed preview or proposal.
* Class 2: other ``push_update``; autonomous proposals within 6 h of expiry are promoted here.
* Class 3: ``open_pr`` previews and skill cards.
* Class 4: autonomous proposals (CR-004 rank score, descending, instead of the target priority).
* Class 5: other ``comment``.

``[📥 Open n]`` re-sends with ``/approve`` semantics and the expiry kept (``AppContext.reissue`` /
``reissue_proposal`` / ``reissue_skill``), so opening an item never moves it. Gate 2 (approval cards)
is listed before gate 1 (proposals). Expired items aren't listed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from ossbot.common.clock import from_iso
from ossbot.common.config import resolve_target
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui.limits import middle_cut

PER_PAGE = 5
PROMOTE_WITHIN = timedelta(hours=6)
Sort = Literal["priority", "oldest"]


@dataclass(frozen=True)
class Item:
    gate: int  # 2 = approval cards, 1 = proposals
    class_rank: int
    expires_at: datetime
    target_priority: int
    created_at: str
    family: Literal["a", "p", "s"]
    subject: str  # cid, or the skill name
    label: str
    score: float = 0.0

    def key(self, sort: Sort) -> tuple[Any, ...]:
        if sort == "oldest":
            return (-self.gate, self.expires_at, self.created_at)
        tie = -self.score if self.class_rank == 4 else -self.target_priority
        return (-self.gate, self.class_rank, self.expires_at, tie, self.created_at)


def _priority(ctx: AppContext, upstream: str) -> int:
    target = resolve_target(ctx.config.targets, upstream)
    return int(getattr(target, "priority", 0) or 0) if target is not None else 0


def items(ctx: AppContext) -> list[Item]:
    now = ctx.clock.now()
    out: list[Item] = []
    for r in ctx.conn.execute(
        "SELECT a.id, a.expires_at, a.created_at, o.kind, o.payload_json, c.id AS cid, c.upstream, c.source,"
        " c.pr_number FROM approvals a JOIN outbound_actions o ON o.id = a.action_id JOIN contributions c"
        " ON c.id = o.cid WHERE a.status IN ('pending','armed')"
    ).fetchall():
        kind = str(r["kind"])
        user = r["source"] == "user"
        if kind == "comment":
            target = json.loads(r["payload_json"]).get("target_number")
            rank = 0 if r["pr_number"] and target == r["pr_number"] else 5
        elif kind == "issue_claim" or user:
            rank = 1
        elif kind == "push_update":
            rank = 2
        else:
            rank = 3
        icon = {"open_pr": "🧩", "push_update": "🔁", "comment": "💬", "issue_claim": "🙋"}[kind]
        what = {"open_pr": "PR preview", "push_update": f"PR #{r['pr_number']} fix", "comment": "comment",
                "issue_claim": "claim"}[kind]  # fmt: skip
        out.append(Item(2, rank, from_iso(r["expires_at"]), _priority(ctx, str(r["upstream"])), str(r["created_at"]),
                        "a", str(r["cid"]), f"{icon} {what} · {middle_cut(str(r['upstream']), 30)}"
                        + (" · 👤" if user else "")))  # fmt: skip
    for r in ctx.conn.execute(
        "SELECT p.id, p.expires_at, p.created_at, p.version, c.id AS cid, c.upstream, c.source, c.issue_number,"
        " c.est_class, c.score FROM proposal_approvals p JOIN contributions c ON c.id = p.cid"
        " WHERE p.status IN ('pending','armed')"
    ).fetchall():
        expires = from_iso(r["expires_at"])
        user = r["source"] == "user"
        rank = 1 if user else (2 if expires - now <= PROMOTE_WITHIN else 4)
        issue = f" #{r['issue_number']}" if r["issue_number"] else ""
        out.append(Item(1, rank, expires, _priority(ctx, str(r["upstream"])), str(r["created_at"]), "p",
                        str(r["cid"]), f"💡 Proposal v{r['version']} · {middle_cut(str(r['upstream']), 28)}{issue}"
                        f" · {r['est_class'] or '?'}" + (" · 👤" if user else ""),
                        float(r["score"] or 0.0)))  # fmt: skip
    for r in ctx.conn.execute(
        "SELECT id, expires_at, created_at, skill, version FROM skill_approvals WHERE status IN ('pending','armed')"
    ).fetchall():
        out.append(Item(2, 3, from_iso(r["expires_at"]), 0, str(r["created_at"]), "s", str(r["skill"]),
                        f"🧠 Skill {middle_cut(str(r['skill']), 30)} v{r['version']}"))  # fmt: skip
    return out


def ordered(ctx: AppContext, sort: Sort = "priority") -> list[Item]:
    return sorted(items(ctx), key=lambda i: i.key(sort))


def view(ctx: AppContext, page: int = 1, sort: Sort = "priority") -> k.View:
    queue = ordered(ctx, sort)
    pages = max(1, -(-len(queue) // PER_PAGE))
    page = min(max(1, page), pages)
    start = (page - 1) * PER_PAGE
    shown = queue[start : start + PER_PAGE]
    lines: list[k.Line] = [(k.Const("📥 "), k.Bold(k.Const("Queue")), k.Const(f" · {len(queue)} open"))]
    gate2 = sum(1 for i in queue if i.gate == 2)
    current_gate = 0
    for n, item in enumerate(shown, start + 1):
        if item.gate != current_gate:
            current_gate = item.gate
            gate1 = len(queue) - gate2
            title = f"Gate 2 · approval cards ({gate2})" if item.gate == 2 else f"Gate 1 · proposals ({gate1})"
            lines.append((k.Bold(k.Const(title)),))
        lines.append((k.Const(f"{n}. "), k.Text(item.label, 90), k.Const(" · expires "),
                      k.Time(int(item.expires_at.timestamp()), item.expires_at.strftime("%d %b %H:%M"))))  # fmt: skip
    if not queue:
        lines.append(k.line("Nothing is waiting for you."))
    opens = [k.button(f"📥 Open {start + i + 1}", "queue_open", {"family": it.family, "subject": it.subject})
             for i, it in enumerate(shown)]  # fmt: skip
    rows: k.Rows = [opens[i : i + 3] for i in range(0, len(opens), 3)]
    mark = {s: "✅ " if sort == s else "" for s in ("oldest", "priority")}
    rows.append([k.button(mark["oldest"] + "⬆️ Oldest", "queue_sort", {"sort": "oldest"}),
                 k.button(mark["priority"] + "🎯 Priority", "queue_sort", {"sort": "priority"})])  # fmt: skip
    if pages > 1:
        rows.append(k.pager("queue_sort", {"sort": sort}, page, pages))
    return k.View(kind="picker", blocks=[k.Block((k.Lines(tuple(lines)),))], rows=rows[:4],
                  state={"page": page, "sort": sort})  # fmt: skip
