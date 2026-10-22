"""Notifications (DESIGN §14.13): priority -> topic, sound and batching; quiet hours, ``/quiet`` and
``/notify``; coalescing and batching.

* **Priorities** (§14.13.1). P0 (kill, anomaly, surface or firewall alerts) always rings and is never
  held or silenced (U7). P1 (cards, a maintainer reply, a blocking failure) rings, silent during quiet
  hours or when its ``/notify`` category is off. P2 (FYIs) is silent and is held in quiet hours or a
  2-minute batch window, then merged into one ``📈 N updates`` message. P3 is digest-only.
* **Rows with buttons are never held or merged** (§14.13.3, AC 49): a row with tokens, an approval or an
  inline keyboard is sent at once, silently when it would otherwise ring. ``Outbox.enqueue`` enforces
  this whatever the policy answers.
* **Toggles mute or batch, they never drop** (§14.13.2): approval, proposal, rescope and ceiling cards
  are always delivered; only the two digests are not generated when off (FEAT-010).
* **Coalescing**: a ``sendMessage`` with a ``coalesce_key`` (``<cid>:<event_kind>``) within 10 minutes
  of the newest sent row with that key edits that message instead of sending a new one.

``Notifier.decide`` is ``Outbox.hold_policy`` (enqueue time), ``Notifier.prepare`` runs just before a
send, and ``Notifier.release`` (once per outbox tick) releases or merges the due held rows and sends
the "🌅 While you were away" summary once quiet hours end.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from ossbot.common.clock import from_iso, to_iso
from ossbot.gatekeeper import db
from ossbot.gatekeeper.outbox import HoldDecision, HoldRequest, OutboxRow
from ossbot.gatekeeper.telegram.escape import esc
from ossbot.gatekeeper.telegram.prefs import UxPrefs
from ossbot.gatekeeper.telegram.ui import ids
from ossbot.gatekeeper.telegram.ui import kit as k

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

BATCH_WINDOW = timedelta(minutes=2)
COALESCE_WINDOW = timedelta(minutes=10)
MERGED_LINE_MAX = 100
MERGED_LINES = 15
MERGED_BODY_MAX = 3000
# /notify categories (§14.13.2): P1 and P2 only; P0 has no toggle.
CATEGORIES: dict[str, str] = {
    "approvals": "Approvals", "proposals": "Proposals", "maintainer": "Maintainer activity", "ci": "CI",
    "stages": "Pipeline stages", "discovery": "Discovery", "digest_daily": "Digest daily",
    "digest_weekly": "Digest weekly", "assistant_back": "Assistant back",
}  # fmt: skip
# System topic per priority: alerts and actions in 🚦 Control, FYIs in 📈 OSS feed (§14.10.1).
THREAD_OF: dict[int, str] = {0: "control", 1: "control", 2: "feed", 3: "feed"}
TAG_RE = re.compile(r"<[^>]+>")
# "anomaly → pause/kill" is P0 (§14.13.1): a pause by the anomaly handler, or one requested by a
# service account (runner, sandboxd, Hermes) rather than a human, is an automatic stop.
AUTO_PAUSE_RE = re.compile(r"⏸ Paused by (anomaly|uid:\d+/(worker|sandbox|requester))\b")


def classify(text: str) -> tuple[int, bool]:
    """(priority, holdable) of a gatekeeper FYI from its fixed leading icon (the alert texts are
    gatekeeper templates). 🛑 kill, 🚨 alerts and automatic pauses are P0, except the explicitly P1
    probe notice; the local-operator-action FYI is a P2 that is never held (§14.13.1)."""
    if text.startswith("🚨 P1"):
        return 1, False
    if text.startswith(("🛑", "🚨")) or AUTO_PAUSE_RE.match(text):
        return 0, False
    if text.startswith("⚠️ Local operator action"):
        return 2, False
    return 2, True


def quiet_hours_end(window: str | None, zone: str, now: datetime) -> datetime | None:
    """The end of the ``HH:MM-HH:MM`` window (may wrap midnight) in ``zone`` when ``now`` is inside it."""
    if not window:
        return None
    start_s, end_s = window.split("-", 1)
    start, end = time.fromisoformat(start_s), time.fromisoformat(end_s)
    local = now.astimezone(ZoneInfo(zone))
    t = local.time().replace(second=0, microsecond=0)
    inside = (start <= t < end) if start < end else (t >= start or t < end)
    if not inside or start == end:
        return None
    end_day = local.date() if (start < end or t < end) else local.date() + timedelta(days=1)
    return datetime.combine(end_day, end, tzinfo=local.tzinfo).astimezone(now.tzinfo)


@dataclass(frozen=True)
class Away:
    cards: int = 0
    messages: int = 0


class Notifier:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.prefs = UxPrefs(ctx.conn, ctx.clock)

    # ------------------------------------------------------------------ state
    def quiet_until(self) -> datetime | None:
        """The later of ``/quiet`` and the profile's quiet hours, when either is active now."""
        now = self.ctx.clock.now()
        manual = self.prefs.until("quiet_until")
        tg = self.ctx.config.profile.telegram
        hours = quiet_hours_end(tg.quiet_hours, tg.timezone, now)
        ends = [w for w in (manual, hours) if w is not None]
        return max(ends) if ends else None

    def toggles(self) -> dict[str, bool]:
        stored = self.prefs.get("notify")
        out = dict.fromkeys(CATEGORIES, True)
        if isinstance(stored, dict):
            out.update({key: bool(v) for key, v in stored.items() if key in CATEGORIES})
        return out

    def set_toggle(self, category: str, on: bool) -> dict[str, bool]:
        if category not in CATEGORIES:
            raise KeyError(category)
        current = self.toggles()
        current[category] = on
        self.prefs.set("notify", current)
        return current

    # ------------------------------------------------------------------ enqueue time
    def decide(self, req: HoldRequest) -> HoldDecision:
        """``Outbox.hold_policy``: whether a row rings and whether it waits (never with buttons)."""
        if req.priority == 0:
            return HoldDecision(silent=False)  # U7: P0 is never silenced or held
        quiet = self.quiet_until()
        muted = req.category is not None and not self.toggles().get(req.category, True)
        if req.priority == 1:
            return HoldDecision(silent=True) if (quiet or muted) else HoldDecision()
        if req.has_buttons or not req.holdable:
            return HoldDecision(silent=True)
        now = self.ctx.clock.now()
        if quiet is not None:
            return HoldDecision(hold_until=quiet, silent=True)
        window = self._window_end(req.thread_key, now)
        if muted:
            return HoldDecision(hold_until=window or now + BATCH_WINDOW, silent=True)
        return HoldDecision(hold_until=window, silent=True)

    def _window_end(self, thread_key: str | None, now: datetime) -> datetime | None:
        """An open batch window of this topic: a held row waiting for its end, or a P2 FYI sent or
        queued within the last 2 minutes (the window ends 2 minutes after it)."""
        thread = thread_key or ""
        held = db.one(self.ctx.conn, "SELECT MIN(next_attempt_at) FROM tg_outbox WHERE status = 'held' AND"
                      " COALESCE(thread_key, '') = ? AND next_attempt_at > ?", (thread, to_iso(now)))  # fmt: skip
        if held is not None and held[0]:
            return from_iso(str(held[0]))
        recent = db.one(self.ctx.conn, "SELECT MAX(next_attempt_at) FROM tg_outbox WHERE priority = 2 AND status IN"
                        " ('sent','pending') AND bind_tokens_json IS NULL AND approval_id IS NULL AND"
                        " COALESCE(thread_key, '') = ? AND next_attempt_at > ?",
                        (thread, to_iso(now - BATCH_WINDOW)))  # fmt: skip
        if recent is not None and recent[0]:
            return from_iso(str(recent[0])) + BATCH_WINDOW
        return None

    # ------------------------------------------------------------------ send time
    def prepare(self, row: OutboxRow) -> OutboxRow:
        """P0 rings; P1 counts towards the "while you were away" summary during quiet hours; a coalescing
        FYI becomes an edit of the newest sent message with its key (§14.13.3)."""
        payload = dict(row.payload)
        if row.priority == 0 and row.method.startswith("send"):
            payload["disable_notification"] = False
        elif row.priority == 1 and row.method.startswith("send") and self.quiet_until() is not None:
            payload["disable_notification"] = True
            self._count_away(row)
        row = replace(row, payload=payload)
        if row.method != "sendMessage" or row.has_buttons:
            return row
        stored = db.one(self.ctx.conn, "SELECT coalesce_key FROM tg_outbox WHERE id = ?", (row.id,))
        key = stored["coalesce_key"] if stored is not None else None
        if not key:
            return row
        since = to_iso(self.ctx.clock.now() - COALESCE_WINDOW)
        earlier = db.one(self.ctx.conn, "SELECT result_message_id FROM tg_outbox WHERE coalesce_key = ? AND status ="
                         " 'sent' AND id != ? AND next_attempt_at >= ? ORDER BY id DESC LIMIT 1",
                         (key, row.id, since))  # fmt: skip
        if earlier is None or earlier["result_message_id"] is None:
            return row  # sent before result ids existed, or none: a new message
        keep = ("chat_id", "text", "parse_mode", "link_preview_options", "entities")
        edit: dict[str, Any] = {key_: payload[key_] for key_ in keep if key_ in payload}
        edit["message_id"] = int(earlier["result_message_id"])
        return replace(row, method="editMessageText", payload=edit)

    def _count_away(self, row: OutboxRow) -> None:
        away = self._away()
        cards = away.cards + (1 if (row.dedupe_key or "").startswith("card:") else 0)
        messages = away.messages + (0 if (row.dedupe_key or "").startswith("card:") else 1)
        self.prefs.set("away", {"cards": cards, "messages": messages})

    def _away(self) -> Away:
        raw = self.prefs.get("away")
        if not isinstance(raw, dict):
            return Away()
        return Away(int(raw.get("cards", 0) or 0), int(raw.get("messages", 0) or 0))

    # ------------------------------------------------------------------ release (tick)
    def release(self) -> int:
        """Release or merge the due held rows per topic; after quiet hours, one P1 summary first."""
        now = self.ctx.clock.now()
        quiet = self.quiet_until()
        conn = self.ctx.conn
        with db.transaction(conn):
            if quiet is not None:  # /quiet was extended, or the profile window is still on
                conn.execute("UPDATE tg_outbox SET next_attempt_at = ? WHERE status = 'held' AND next_attempt_at <= ?",
                             (to_iso(quiet), to_iso(now)))  # fmt: skip
                return 0
            away = self._away()
            if away.cards or away.messages:
                self.prefs.set("away", {"cards": 0, "messages": 0})
                self.ctx.outbox.enqueue("sendMessage", {"chat_id": self.ctx.telegram_chat_id,
                                                        "text": away_text(away)},
                                        priority=1, thread_key="control", holdable=False)  # fmt: skip
            due = conn.execute("SELECT * FROM tg_outbox WHERE status = 'held' AND next_attempt_at <= ? ORDER BY id",
                               (to_iso(now),)).fetchall()  # fmt: skip
            groups: dict[str, list[Any]] = {}
            for r in due:
                groups.setdefault(str(r["thread_key"] or ""), []).append(r)
            released = 0
            for thread, rows in groups.items():
                if len(rows) == 1:
                    conn.execute("UPDATE tg_outbox SET status = 'pending' WHERE id = ?", (rows[0]["id"],))
                else:
                    merged = self._merge(thread or None, rows)
                    marks = ",".join("?" * len(rows))
                    conn.execute(f"UPDATE tg_outbox SET status = 'merged', merged_into = ? WHERE id IN ({marks})",  # noqa: S608
                                 (merged, *[r["id"] for r in rows]))  # fmt: skip
                released += len(rows)
        return released

    def _merge(self, thread: str | None, rows: list[Any]) -> int | None:
        """One ``📈 N updates`` row: a line each, the full list in an expandable quote, ``📥 Queue``
        when an approval is waiting (§14.13.3)."""
        texts = [plain_text(json.loads(r["payload_json"])) for r in rows]
        lines: list[k.Line] = [(k.Const("📈 "), k.Bold(k.Const(f"{len(rows)} updates")))]
        lines += [ids.spans(f"• {first_line(t)}") for t in texts[:MERGED_LINES]]  # ids one tap to copy
        if len(texts) > MERGED_LINES:
            lines.append(k.line(f"+{len(texts) - MERGED_LINES} more below"))
        body = "\n\n".join(texts)[:MERGED_BODY_MAX]
        blocks = [k.Block((k.Lines(tuple(lines)),), name="lines"),
                  k.Block((k.Quote(tuple(ids.spans(x) for x in body.split("\n")), expandable=True),), required=False,
                          priority=1, name="full")]  # fmt: skip
        rows_: k.Rows = []
        pending = db.one(self.ctx.conn, "SELECT COUNT(*) FROM approvals WHERE status IN ('pending','armed')")
        if pending is not None and int(pending[0]):
            rows_ = [[k.button("📥 Queue", "dash_queue")]]
        from ossbot.gatekeeper.telegram.ui import send as ui_send  # noqa: PLC0415 - ui.send imports the outbox

        thread_name: Any = thread if thread in {"control", "reviews", "feed", "assistant"} else None
        return ui_send.deliver(self.ctx, k.View(kind="fyi", blocks=blocks, rows=rows_), thread=thread_name,
                               silent=True, holdable=False)  # fmt: skip


def away_text(away: Away) -> str:
    parts = []
    if away.cards:
        parts.append(f"{away.cards} card{'s' if away.cards != 1 else ''}")
    if away.messages:
        parts.append(f"{away.messages} message{'s' if away.messages != 1 else ''}")
    return "🌅 While you were away: " + ", ".join(parts)


def plain_text(payload: dict[str, Any]) -> str:
    text = str(payload.get("text") or "")
    if payload.get("parse_mode") == "HTML":
        text = TAG_RE.sub("", text).replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace(
            "&amp;", "&")  # fmt: skip
    return text


def first_line(text: str) -> str:
    line = text.split("\n", 1)[0].strip()
    return line if len(line) <= MERGED_LINE_MAX else line[: MERGED_LINE_MAX - 1] + "…"


def quiet_text(until: datetime) -> str:
    return f"🌙 Quiet until {esc(until.strftime('%H:%M'))} (P0 alerts still ring)"
