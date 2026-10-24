"""The daily and weekly digest (DESIGN §14.13.4, §14.19 item 1; AC 42).

Daily at ``telegram.digest.daily`` and weekly at ``telegram.digest.weekly`` (local time,
``telegram.timezone``), both to 📈 OSS feed as silent P2; a kind whose ``/notify`` toggle is off is not
generated at all. One ``digest_runs`` row per ``(kind, period_start)`` makes each run happen once.

Content: LIVE contributions (opened, merged, closed, rejected) with dry-run rehearsals counted apart,
pending approvals and the soonest expiry, PRs waiting on maintainers vs on you, review rounds, followers
and stars gained (gatekeeper GitHub reads, snapshotted in ``digest_runs.stats_json``; "gained" is the
delta to the previous snapshot), the streak, top error classes, Hermes usage, and the merge rate by size
class (weekly, and daily when a proposal or merge happened that day). No button changes the size
preference; the digest only names ``/size``.

Discovery section (discovery fix 5c and 10): cycles finished and cut by reason (the ``discovery_cycle``
audit rows the runner's reports left), candidates found, new ``source = 'discover'`` items, their gate-1
proposals, the top terminal drop reasons (vet and scout), the scout's on-main skips (always shown, 0
included) and discovery stage error classes, all from the gatekeeper's own tables; reasons are ``k.Text``.
A period without a finished cycle shows a warning line.

Egress notes (ADR-0025): a "🌐 Egress notes" block with the period's count and up to three lines (volume notes of
``sandbox_full`` web jobs, floor notes such as loopback names) when there are any; absent otherwise.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from ossbot.common.clock import from_iso, to_iso
from ossbot.gatekeeper import db, outcomes
from ossbot.gatekeeper.egress_notes import EgressNotes
from ossbot.gatekeeper.telegram.runtime import ui_of
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

log = logging.getLogger(__name__)
Kind = Literal["daily", "weekly"]
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WINDOW_DAYS = 90
LIVE_EVENTS = ("open_pr", "push_update", "comment")


@dataclass(frozen=True)
class Period:
    kind: Kind
    start: date  # local date the run belongs to (daily: today; weekly: the configured weekday)
    since: datetime  # UTC start of the counted window
    until: datetime


def _zone(ctx: AppContext) -> ZoneInfo:
    return ZoneInfo(ctx.config.profile.telegram.timezone)


def due(ctx: AppContext, now: datetime | None = None) -> list[Period]:
    """The digests whose local time has come and that have no ``digest_runs`` row yet."""
    now = now or ctx.clock.now()
    zone = _zone(ctx)
    local = now.astimezone(zone)
    cfg = ctx.config.telegram.digest
    toggles = ui_of(ctx).notifier.toggles()
    out: list[Period] = []
    hh, mm = (int(x) for x in cfg.daily.split(":"))
    if toggles.get("digest_daily", True) and local.time() >= time(hh, mm):
        start = local.date()
        out.append(Period("daily", start, (now - timedelta(days=1)), now))
    day, at = cfg.weekly.split(" ")
    wh, wm = (int(x) for x in at.split(":"))
    if toggles.get("digest_weekly", True) and DAYS[local.weekday()] == day and local.time() >= time(wh, wm):
        out.append(Period("weekly", local.date(), now - timedelta(days=7), now))
    return [p for p in out if db.one(ctx.conn, "SELECT 1 FROM digest_runs WHERE kind = ? AND period_start = ?",
                                     (p.kind, p.start.isoformat())) is None]  # fmt: skip


def _count(ctx: AppContext, sql: str, params: tuple[Any, ...]) -> int:
    return int(ctx.conn.execute(sql, params).fetchone()[0])


def live_days(ctx: AppContext) -> set[date]:
    """Local days with a LIVE contribution event: an executed action or a merge."""
    zone = _zone(ctx)
    stamps = [str(r[0]) for r in ctx.conn.execute(
        "SELECT executed_at FROM outbound_actions WHERE status = 'done' AND executed_at IS NOT NULL"
        " AND kind IN ('open_pr', 'push_update', 'comment')")]  # fmt: skip
    stamps += [str(r[0]) for r in ctx.conn.execute(
        "SELECT finished_at FROM outcomes WHERE result = 'merged' AND finished_at IS NOT NULL")]  # fmt: skip
    return {from_iso(s).astimezone(zone).date() for s in stamps}


def _previous_stats(ctx: AppContext, kind: Kind) -> dict[str, Any]:
    row = db.one(ctx.conn, "SELECT stats_json FROM digest_runs WHERE kind = ? AND sent_at IS NOT NULL"
                           " ORDER BY period_start DESC LIMIT 1", (kind,))  # fmt: skip
    return json.loads(row["stats_json"]) if row is not None else {}


async def github_stats(ctx: AppContext, reader: Any) -> dict[str, int]:
    """Followers and stars (read-only GETs with the gatekeeper's read client); {} when unavailable."""
    login = ctx.config.profile.github_login
    if reader is None or not login:
        return {}
    try:
        followers = await reader.followers(login)
        stars = await reader.stars(login)
    except Exception:  # noqa: BLE001 - a failed read only drops the two lines
        log.warning("digest GitHub stats unavailable", extra={"event": "digest_stats_failed"})
        return {}
    return {"followers": followers, "stars": stars} if followers is not None else {"stars": stars}


def collect(ctx: AppContext, period: Period, gh: dict[str, int]) -> dict[str, Any]:
    since, until = to_iso(period.since), to_iso(period.until)
    span = (since, until)
    stats: dict[str, Any] = {
        "opened": _count(ctx, "SELECT COUNT(*) FROM outcomes WHERE pr_opened_at >= ? AND pr_opened_at < ?", span),
        "merged": _count(ctx, "SELECT COUNT(*) FROM outcomes WHERE result = 'merged' AND finished_at >= ?"
                              " AND finished_at < ?", span),
        "closed": _count(ctx, "SELECT COUNT(*) FROM outcomes WHERE result = 'closed' AND finished_at >= ?"
                              " AND finished_at < ?", span),
        "rejected": _count(ctx, "SELECT COUNT(*) FROM contributions WHERE state IN ('REJECTED','PROPOSAL_REJECTED')"
                                " AND updated_at >= ? AND updated_at < ?", span),
        "dry_run": _count(ctx, "SELECT COUNT(*) FROM outcomes WHERE result = 'dry_run' AND finished_at >= ?"
                               " AND finished_at < ?", span),
        "proposed": _count(ctx, "SELECT COUNT(*) FROM outcomes WHERE proposed_at >= ? AND proposed_at < ?", span),
        "pending": _count(ctx, "SELECT COUNT(*) FROM approvals WHERE status IN ('pending','armed')", ()),
        "waiting_maintainers": _count(ctx, "SELECT COUNT(*) FROM contributions WHERE state = 'WATCHING'", ()),
        "waiting_you": _count(ctx, "SELECT COUNT(*) FROM approvals a JOIN outbound_actions o ON o.id = a.action_id"
                                   " WHERE a.status IN ('pending','armed') AND o.kind IN ('comment','push_update')",
                              ()),
        "review_rounds": _count(ctx, "SELECT COALESCE(SUM(watch_rounds), 0) FROM contributions WHERE state ="
                                     " 'WATCHING'", ()),
        "turns": _count(ctx, "SELECT COUNT(*) FROM chat_turns WHERE started_at >= ? AND started_at < ?", span),
        "hermes_demoted": ctx.audit.count_since("hermes_p1_demoted", since),
        **gh,
    }  # fmt: skip
    soonest = db.one(ctx.conn, "SELECT MIN(expires_at) AS e FROM approvals WHERE status IN ('pending','armed')")
    stats["soonest_expiry"] = soonest["e"] if soonest is not None else None
    models = Counter(
        str(r[0])
        for r in ctx.conn.execute(
            "SELECT model FROM chat_turns WHERE started_at >= ? AND started_at < ? AND model IS NOT NULL", span
        )
    )
    stats["models"] = dict(models.most_common(3))
    errors = Counter(
        str(r[0])
        for r in ctx.conn.execute(
            "SELECT error_class FROM stage_runs WHERE error_class IS NOT NULL AND started_at >= ?", (since,)
        )
    )
    stats["errors"] = dict(errors.most_common(3))
    stats["streak"] = outcomes.streak(live_days(ctx), period.start)
    stats["discovery"] = discovery_stats(ctx, span)
    stats["egress_notes"] = egress_notes_stats(ctx, span)
    return stats


NOTES_SHOWN = 3
GIB = 1024 * 1024 * 1024
MIB = 1024 * 1024


def egress_notes_stats(ctx: AppContext, span: tuple[str, str]) -> dict[str, Any]:
    """ADR-0025: the period's egress notes (volume notes of ``sandbox_full`` web jobs, floor notes such as loopback
    names): the count and the newest ``NOTES_SHOWN`` as plain numbers (rendered escaped by ``egress_notes_block``)."""
    notes = [n for n in EgressNotes(ctx.conn, ctx.clock, ctx.audit).since(span[0]) if n.created_at < span[1]]
    items = []
    for note in notes[:NOTES_SHOWN]:
        row = db.one(ctx.conn, "SELECT upstream, issue_number FROM contributions WHERE id = ?", (note.cid,))
        subject = note.cid if row is None else str(row["upstream"]) + (
            f"#{row['issue_number']}" if row["issue_number"] else "")  # fmt: skip
        s = note.summary
        items.append({"subject": subject, "stage": s.stage, "kind": s.kind, "breached": list(s.breached),
                      "hosts": s.hosts, "max_hosts": s.limits.max_hosts, "up": s.up_bytes,
                      "max_up_mb": s.limits.max_upload_mb, "down": s.down_bytes,
                      "max_down_mb": s.limits.max_download_mb, "notes": len(s.notes)})  # fmt: skip
    return {"count": len(notes), "items": items}


def _bytes(n: int) -> str:
    return f"{n / GIB:.1f} GB" if n >= GIB else f"{n // MIB} MB"


def egress_notes_block(notes: dict[str, Any]) -> k.Block | None:
    """🌐 Egress notes: the count and up to three lines ``repo#N · validate · 74 hosts (> 60) · ↓ 7.2 GB (> 6 GB)``;
    None when the period had none."""
    count = int(notes.get("count") or 0)
    if not count:
        return None
    lines: list[k.Line] = [(k.Const("🌐 "), k.Bold(k.Const("Egress notes")),
                            k.Const(f" · {_n(count, 'note')} · nothing was held"))]  # fmt: skip
    for item in list(notes.get("items") or [])[:NOTES_SHOWN]:
        parts: list[str] = []
        breached = set(item.get("breached") or [])
        if "hosts" in breached:
            parts.append(f"{int(item['hosts'])} hosts (> {int(item['max_hosts'])})")
        if "upload" in breached:
            parts.append(f"↑ {_bytes(int(item['up']))} (> {int(item['max_up_mb'])} MB)")
        if "download" in breached:
            parts.append(f"↓ {_bytes(int(item['down']))} (> {_bytes(int(item['max_down_mb']) * MIB)})")
        if item.get("notes"):
            parts.append(_n(item["notes"], "floor note"))
        tail = " · ".join([str(item.get("stage") or "-"), *parts])
        lines.append((k.Text(str(item.get("subject") or "?"), 160), k.Const(" · "), k.Text(tail, 200)))
    return k.Block((k.Lines(tuple(lines)),), required=False, priority=2, name="egress_notes")


ON_MAIN = ("already_fixed_on_main", "not_reproducible_on_main")  # scout's check-main skips (fix 10)
DROPS_SHOWN = 6


def discovery_stats(ctx: AppContext, span: tuple[str, str]) -> dict[str, Any]:
    """Discovery fix 5c and 10, from the gatekeeper's own tables only: ``discovery_cycle`` audit rows
    (finished, cut by reason, candidates found), ``source = 'discover'`` items created, their terminal
    drops by ``state_reason`` (vet and scout, the on-main skips counted apart, always), gate-1 proposals
    and stage error classes in the window."""
    finished, candidates = 0, 0
    cuts: Counter[str] = Counter()
    for (data,) in ctx.conn.execute("SELECT data_json FROM audit_events WHERE event = 'discovery_cycle' AND ts >= ?"
                                    " AND ts < ?", span):  # fmt: skip
        doc = json.loads(data)
        if not isinstance(doc, dict):
            continue
        if doc.get("outcome") == "finished":
            finished += 1
            candidates += int(doc.get("candidates") or 0) + int(doc.get("open_candidates") or 0)
        else:
            cuts[str(doc.get("cut_reason"))] += 1
    drops = Counter({
        str(r[0]): int(r[1])
        for r in ctx.conn.execute(
            "SELECT state_reason, COUNT(*) FROM contributions WHERE source = 'discover' AND state IN"
            " ('SKIPPED','FAILED','ABANDONED') AND state_reason IS NOT NULL AND updated_at >= ? AND updated_at < ?"
            " GROUP BY state_reason", span)
    })  # fmt: skip
    errors = Counter({
        str(r[0]): int(r[1])
        for r in ctx.conn.execute(
            "SELECT s.error_class, COUNT(*) FROM stage_runs s JOIN contributions c ON c.id = s.cid WHERE"
            " c.source = 'discover' AND s.error_class IS NOT NULL AND s.started_at >= ? AND s.started_at < ?"
            " GROUP BY s.error_class", span)
    })  # fmt: skip
    return {
        "finished": finished,
        "cut": dict(cuts.most_common()),
        "candidates": candidates,
        "created": _count(ctx, "SELECT COUNT(*) FROM contributions WHERE source = 'discover' AND created_at >= ?"
                               " AND created_at < ?", span),
        "proposals": _count(ctx, "SELECT COUNT(*) FROM outcomes WHERE source = 'discover' AND proposed_at >= ?"
                                 " AND proposed_at < ?", span),
        "drops": dict(Counter({r: n for r, n in drops.items() if r not in ON_MAIN}).most_common(DROPS_SHOWN)),
        "on_main": {r: drops.get(r, 0) for r in ON_MAIN},
        "errors": dict(errors.most_common(3)),
    }  # fmt: skip


CUT_SHOWN = {"search_limit": "search limit", "core_limit": "core limit", "github_unavailable": "GitHub down",
             "stopped": "stopped", "gatekeeper": "gatekeeper"}  # fmt: skip
REASON_SHOWN = 64


def _n(count: Any, noun: str) -> str:
    return f"{int(count)} {noun}{'' if int(count) == 1 else 's'}"


def discovery_block(disc: dict[str, Any]) -> k.Block:
    """🔁 cycles · 🔎 found · 🚫 dropped · 🌿 on main · ❌ errors; a warning line when no cycle finished."""
    cut = {str(r): int(n) for r, n in dict(disc.get("cut") or {}).items()}
    cycles = f"{disc['finished']} finished · {sum(cut.values())} cut"
    if cut:
        cycles += " (" + " · ".join(f"{CUT_SHOWN.get(r, r)} ×{n}" for r, n in cut.items()) + ")"
    lines: list[k.Line] = [
        (k.Const("🔎 "), k.Bold(k.Const("Discovery"))),
        k.fact("🔁 Cycles:", cycles),
        k.fact("🔎 Found:", f"{_n(disc['candidates'], 'candidate')} · {_n(disc['created'], 'new item')} ·"
                           f" {_n(disc['proposals'], 'proposal')}"),
    ]  # fmt: skip
    drops = dict(disc.get("drops") or {})
    if drops:
        spans: list[k.Span] = [k.Const("🚫 Dropped: ")]
        for i, (reason, n) in enumerate(drops.items()):
            spans += [*([k.Const(" · ")] if i else []), k.Text(str(reason), REASON_SHOWN), k.Const(f" ×{int(n)}")]
        lines.append(tuple(spans))
    on_main = dict(disc.get("on_main") or {})
    lines.append(k.fact("🌿 On main:", f"{int(on_main.get(ON_MAIN[0], 0))} already fixed ·"
                                      f" {int(on_main.get(ON_MAIN[1], 0))} not reproducible"))  # fmt: skip
    errors = dict(disc.get("errors") or {})
    if errors:
        spans = [k.Const("❌ Discovery errors: ")]
        for i, (error, n) in enumerate(errors.items()):
            spans += [*([k.Const(", ")] if i else []), k.Text(str(error), REASON_SHOWN), k.Const(f" ×{int(n)}")]
        lines.append(tuple(spans))
    if not disc["finished"]:
        lines.append(k.line("⚠️ No discovery cycle finished in this period"))
    return k.Block((k.Lines(tuple(lines)),), required=False, priority=2, name="discovery")


def _gained(stats: dict[str, Any], prev: dict[str, Any], key: str) -> str | None:
    if key not in stats:
        return None
    if key not in prev:
        return f"{stats[key]}"
    delta = int(stats[key]) - int(prev[key])
    return f"{stats[key]} ({'+' if delta >= 0 else ''}{delta})"


def view(ctx: AppContext, period: Period, stats: dict[str, Any], prev: dict[str, Any]) -> k.View:
    title = "Daily digest" if period.kind == "daily" else "Weekly digest"
    lines: list[k.Line] = [
        k.header("📈", title, period.start.isoformat()),
        k.fact("🚀 LIVE:", f"{stats['opened']} opened · {stats['merged']} merged · {stats['closed']} closed · "
                          f"{stats['rejected']} rejected"),
        k.fact("🧪 Dry runs:", str(stats["dry_run"])),
        k.fact("✅ Pending approvals:", str(stats["pending"])),
        k.fact("⏳ Waiting:", f"{stats['waiting_maintainers']} on maintainers · {stats['waiting_you']} on you"),
        k.fact("🔁 Review rounds:", str(stats["review_rounds"])),
    ]  # fmt: skip
    if stats.get("soonest_expiry"):
        at = from_iso(str(stats["soonest_expiry"]))
        lines.append((k.Const("⌛ Soonest expiry "), k.Time(int(at.timestamp()), at.strftime("%Y-%m-%d %H:%M"))))
    for key, label in (("followers", "👥 Followers:"), ("stars", "⭐ Stars:")):
        shown = _gained(stats, prev, key)
        if shown is not None:
            lines.append(k.fact(label, shown))
    lines.append(k.fact("🔥 Streak:", f"{stats['streak']} day{'s' if stats['streak'] != 1 else ''}"))
    if stats["errors"]:
        lines.append(k.fact("❌ Top errors:", ", ".join(f"{e} ×{n}" for e, n in stats["errors"].items())))
    models = ", ".join(f"{m} ×{n}" for m, n in stats["models"].items())
    lines.append(k.fact("🤖 Hermes:", f"{stats['turns']} turns" + (f" · {models}" if models else "")))
    if stats["hermes_demoted"]:
        lines.append(k.line(f"🤖 Hermes asked for {stats['hermes_demoted']} more loud notices today"))
    blocks = [k.Block((k.Lines(tuple(lines)),), name="facts")]
    if isinstance(stats.get("discovery"), dict):  # older stats_json rows have no discovery section
        blocks.append(discovery_block(stats["discovery"]))
    notes_block = egress_notes_block(stats["egress_notes"]) if isinstance(stats.get("egress_notes"), dict) else None
    if notes_block is not None:  # ADR-0025: absent when the period had no egress note
        blocks.append(notes_block)
    if period.kind == "weekly" or stats["proposed"] or stats["merged"]:
        since = to_iso(ctx.clock.now() - timedelta(days=WINDOW_DAYS))
        rows = outcomes.merge_rate_rows(ctx.conn, since)
        if rows:
            table = k.TableEl(
                ("class", "proposed", "started", "PRs", "merged", "rate"),
                tuple((r.label, str(r.proposed), str(r.started), str(r.prs), str(r.merged), r.rate) for r in rows),
            )
            limit = ctx.discovery_prefs.effective().autonomous_max_class
            blocks.append(k.Block((
                k.Lines(((k.Const("📏 "), k.Bold(k.Const("By size")), k.Const(" · last 90 days · LIVE only")),)),
                table, k.Lines((k.line(f"Auto size ≤ {limit} · tune with /size"),)),
            ), required=False, priority=1, name="by_size"))  # fmt: skip
    return k.View(kind="digest", blocks=blocks, rich=True)


async def run(ctx: AppContext, period: Period, reader: Any) -> int | None:
    """Collect, render, record and send one digest (the ``digest_runs`` row first: it runs once)."""
    prev = _previous_stats(ctx, period.kind)
    stats = collect(ctx, period, await github_stats(ctx, reader))
    with db.transaction(ctx.conn):
        cur = ctx.conn.execute("INSERT INTO digest_runs (kind, period_start, sent_at, stats_json) VALUES (?, ?, ?, ?)"
                               " ON CONFLICT(kind, period_start) DO NOTHING",
                               (period.kind, period.start.isoformat(), to_iso(ctx.clock.now()),
                                json.dumps(stats, sort_keys=True)))  # fmt: skip
        if not cur.rowcount:
            return None
    return ui_send.deliver(ctx, view(ctx, period, stats, prev), thread="feed", priority=2, silent=True,
                           dedupe_key=f"digest:{period.kind}:{period.start.isoformat()}",
                           category=f"digest_{period.kind}")  # fmt: skip


async def tick(ctx: AppContext, reader: Any) -> int:
    sent = 0
    for period in due(ctx):
        if await run(ctx, period, reader) is not None:
            sent += 1
    return sent


def now_period(ctx: AppContext, kind: Kind) -> Period:
    """``📨 Send now``: today's period of ``kind`` (a second Send now the same day sends nothing new)."""
    now = ctx.clock.now()
    days = 1 if kind == "daily" else 7
    return Period(kind, now.astimezone(_zone(ctx)).date(), now - timedelta(days=days), now)
