"""The pinned live dashboard in 🚦 Control (DESIGN §14.10.3, D3, D15, AC 14, AC 15; mockup 06).

One classic message (≤ 1,500 characters) recorded as ``tg_views(kind='dashboard')`` and pinned
silently. ``tick`` edits it at most once per ``dashboard.min_edit_interval_s`` (30 s) and only when the
rendered content's sha256 changed, and skips while the outbox has due P0/P1 rows. A deleted
dashboard ("message to edit not found") is re-sent and pinned. Its footer is a relative ``<tg-time>``,
so a stale dashboard visibly ages even while the bot is unreachable.

The buttons are kit tokens bound to the dashboard message and reused across edits (re-minted when
they get within a day of expiry): Refresh, Approvals, Pause or Resume…, Quiet 1h, Down… and the
two-tap Kill (the second tap is a 10-s single-use ``danger_second_tap`` token, D14). With
``telegram.yaml command_shortcuts`` on, two more rows carry Work, Size, Filters, Model and Help
(``telegram/shortcuts.py``).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from ossbot.common import models as model_cfg
from ossbot.common.clock import from_iso, to_iso
from ossbot.common.hashing import sha256_text
from ossbot.common.ids import new_token
from ossbot.gatekeeper import db, egress_status, ratelimit
from ossbot.gatekeeper.anchors import last_anchor_time
from ossbot.gatekeeper.outbox import OutboxRow, SendError
from ossbot.gatekeeper.telegram.prefs import UxPrefs
from ossbot.gatekeeper.telegram.ui import classic, icons, ids
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

log = logging.getLogger(__name__)
DASHBOARD_MAX = 1500
TOKEN_TTL = timedelta(hours=72)
TOKEN_REFRESH = timedelta(hours=24)
KILL_SECOND_TAP = timedelta(seconds=10)
PURPOSES = ("dash_refresh", "dash_queue", "dash_pause", "dash_resume", "quiet_set", "dash_down", "dash_kill")
# telegram.yaml command_shortcuts (telegram/shortcuts.py): one more row of command buttons
SHORTCUT_PURPOSES = ("sc_work", "sc_size", "sc_filters", "sc_model", "sc_help")
RUNNING_LINES = 3


def _unix(ts: datetime) -> int:
    return int(ts.timestamp())


def _hash(value: str) -> k.Span:
    """A config or audit head (12 hex) one tap to copy; an unset or odd value stays plain code."""
    short = (value or "")[:12]
    return k.Id(short, "sha") if ids.valid(short, "sha") else k.Code(k.Const(short))


def _parse_ts(value: str | None) -> datetime | None:
    try:
        return from_iso(value) if value else None
    except ValueError:
        return None


class Dashboard:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._last_edit: datetime | None = None
        self._resent_to_control = False  # once per start (on_sent)

    # ------------------------------------------------------------------ content
    def state_label(self) -> tuple[str, str]:
        """(icon, label): the named states of §14.10.3, incl. the two down states (CR-001)."""
        view = self.ctx.control.view()
        info = self.ctx.control.lifecycle()
        if view.state == "killed":
            return icons.KILLED, f"KILLED ({view.kill_level or 'soft'})"
        if view.state == "paused":
            return icons.PAUSED, "PAUSED"
        if info is not None and info.state == "draining":
            return icons.DRAINING, "DRAINING"
        if info is not None:
            return icons.DOWN, f"DOWN ({'workers' if info.scope == 'workers' else 'graceful'})"
        return icons.UP, "UP"

    def header(self) -> k.Line:
        icon, label = self.state_label()
        dry_run = self.ctx.control.view().dry_run
        return (k.Const(f"{icon} "), k.Bold(k.Const("ossbot")), k.Const(f" · {label} · "), k.mode_badge(dry_run))

    def lines(self) -> list[k.Line]:
        ctx = self.ctx
        conn = ctx.conn
        now = ctx.clock.now()
        prefs = UxPrefs(conn, ctx.clock)
        control = ctx.control.view()
        tier = ctx.control.hermes_tier(ctx.config.policy.hermes.capability_tier_default)
        rich_pref = prefs.get("rich_views", ctx.config.telegram.rich_views_default)
        size = ctx.discovery_prefs.effective(None).autonomous_max_class
        facts = f"Hermes: {tier} · Rich views: {rich_pref} · Auto size ≤ {size}"
        out: list[k.Line] = []
        if control.state != "running":  # why it is paused or killed, and since when (old /status line)
            reason: list[k.Span] = [k.Const("Reason: "), k.Text(control.reason or "-", 200)]
            since = _parse_ts(control.since)
            if since is not None:
                reason += [k.Const(" · since "), k.Time(_unix(since), since.strftime("%d %b %H:%M"))]
            out.append(tuple(reason))
        # build-env design §9.2: mode, implement network (active or not), floor detector, public floor
        out.append(k.line(egress_status.status_line(ctx)))
        out.append(k.line(facts))
        running = conn.execute(
            "SELECT cid, stage, started_at FROM stage_runs WHERE status = 'running' ORDER BY started_at"
        ).fetchall()
        if running:
            items = []
            for r in running[:RUNNING_LINES]:
                mins = max(0, int((now - from_iso(r["started_at"])).total_seconds() // 60)) if r["started_at"] else 0
                items.append(f"{r['cid']} {r['stage']} {mins}m")
            more = f" +{len(running) - RUNNING_LINES} more" if len(running) > RUNNING_LINES else ""
            out.append(k.line(f"Running: {' · '.join(items)}{more}"))
        else:
            out.append(k.line("Running: nothing"))
        pending = int(conn.execute("SELECT COUNT(*) FROM approvals WHERE status IN ('pending','armed')").fetchone()[0])
        proposals = int(conn.execute(
            "SELECT COUNT(*) FROM proposal_approvals WHERE status IN ('pending','armed')").fetchone()[0])  # fmt: skip
        oldest = conn.execute(
            "SELECT MIN(expires_at) FROM (SELECT expires_at FROM approvals WHERE status IN ('pending','armed')"
            " UNION ALL SELECT expires_at FROM proposal_approvals WHERE status IN ('pending','armed'))"
        ).fetchone()[0]
        approvals: list[k.Span] = [k.Const(f"Approvals: {pending} pending · {proposals} proposal")]
        if oldest:
            when = from_iso(oldest)
            approvals += [k.Const(" · oldest expires "), k.Time(_unix(when), when.strftime("%d %b %H:%M"))]
        out.append(tuple(approvals))
        counts = {str(r[0]): int(r[1]) for r in conn.execute(
            "SELECT state, COUNT(*) FROM contributions WHERE state IN ('VETTED','WATCHING','PROPOSED')"
            " GROUP BY state")}  # fmt: skip
        out.append(k.line(f"Queue: {counts.get('VETTED', 0)} vetted · {counts.get('WATCHING', 0)} watching"
                          f" · {counts.get('PROPOSED', 0)} proposed"))  # fmt: skip
        limits = ctx.config.policy.limits
        used = {kind: ratelimit.used_in_window(conn, ctx.clock, kind) for kind in ("open_pr", "comment", "issue_claim")}
        today = (f"Today: PRs {used['open_pr']}/{limits.prs_per_24h} · comments {used['comment']}/"
                 f"{limits.comments_per_24h} · claims {used['issue_claim']}/{limits.issue_claims_per_24h}")  # fmt: skip
        out.append(k.line(today))
        poll = ctx.poll_stats.as_dict()
        tg: list[k.Span] = [k.Const("Telegram: last poll ")]
        if poll.get("last_ok_at"):
            when = from_iso(str(poll["last_ok_at"]))
            tg.append(k.Time(_unix(when), when.strftime("%H:%M")))
        else:
            tg.append(k.Const("-"))
        tg.append(k.Const(f" · {poll.get('reconnects', 0)} reconnect(s)"))
        out.append(tuple(tg))
        out.append(k.line(self._models()))
        # DESIGN §12.7: the config and audit heads with the last root anchor time
        audit: list[k.Span] = [k.Const("Config "), _hash(control.config_hash), k.Const(" · audit "),
                               _hash(control.audit_head), k.Const(" · anchor ")]  # fmt: skip
        anchored = last_anchor_time(ctx.paths.audit_anchors_log)
        audit.append(k.Time(_unix(anchored), anchored.strftime("%H:%M")) if anchored else k.Const("none yet"))
        out.append(tuple(audit))
        return out

    def _models(self) -> str:
        try:
            cfg = self.ctx.models.read()
            names = {role: model_cfg.requested(cfg, role)[0] for role in ("chat", "coder", "vision")}
        except (OSError, ValueError, AttributeError):  # models.yaml unreadable: say so, never fail the dashboard
            return "Models: unavailable"
        return f"Models: chat {names['chat']} · coder {names['coder']} · vision {names['vision']}"

    def render(self, tokens: dict[str, str], *, kill_armed: str | None = None) -> k.View:
        """The pinned dashboard: rows bound to its message's reused token set."""
        view = self.view(tokens, kill_armed=kill_armed)
        view.state = {"tokens": tokens}
        return view

    def view(self, tokens: dict[str, str] | None = None, *, kill_armed: str | None = None,
             kind: str = "dashboard") -> k.View:  # fmt: skip
        """The one dashboard builder, for the pinned dashboard and for ``/status`` (kind ``status``).
        ``tokens`` None = fresh buttons (same labels, purposes and order), minted and bound to the new
        message when it is sent (``ui.send.deliver``), so a /status copy never shares the pinned tokens."""
        now = self.ctx.clock.now()
        blocks = [
            k.Block((k.Lines((self.header(),)),), name="header"),
            k.Block((k.Lines(tuple(self.lines())),), name="facts"),
        ]
        updated = k.FooterEl((k.Const("Updated "), k.Time(_unix(now), now.strftime("%H:%M"))))
        blocks.append(k.Block((updated,), name="footer"))
        return k.View(kind=kind, blocks=blocks, rows=self.rows(tokens, kill_armed=kill_armed))

    def rows(self, tokens: dict[str, str] | None, *, kill_armed: str | None = None) -> k.Rows:
        ctx = self.ctx
        view = ctx.control.view()
        pending = int(ctx.conn.execute(
            "SELECT (SELECT COUNT(*) FROM approvals WHERE status IN ('pending','armed')) +"
            " (SELECT COUNT(*) FROM proposal_approvals WHERE status IN ('pending','armed'))"
        ).fetchone()[0])  # fmt: skip

        def btn(label: str, purpose: str, *, style: k.Style | None = None) -> k.ButtonSpec:
            if tokens is not None:
                return k.bound(label, tokens[purpose], style=style)
            return k.button(label, purpose, {"hours": 1} if purpose == "quiet_set" else {}, style=style, ttl=TOKEN_TTL)

        if view.state != "running":
            pause = btn("▶️ Resume…", "dash_resume")
        else:
            pause = btn("⏸ Pause", "dash_pause")
        if kill_armed:
            last = [k.bound("🛑 Tap again to kill (10 s)", kill_armed, style="danger")]
        else:
            last = [btn("⬇️ Down…", "dash_down"), btn("🛑 Kill", "dash_kill", style="danger")]
        rows = [
            [btn("🔄 Refresh", "dash_refresh"), btn(f"📥 Approvals ({pending})", "dash_queue")],
            [pause, btn("🌙 Quiet 1h", "quiet_set")],
        ]
        if ctx.config.telegram.command_shortcuts:  # ≤ 3 buttons per row (ui/limits.py), so two rows
            rows += [[btn("🛠 Work", "sc_work"), btn("📏 Size", "sc_size"), btn("🔎 Filters", "sc_filters")],
                     [btn("🤖 Model", "sc_model"), btn("❓ Help", "sc_help")]]  # fmt: skip
        return [*rows, last]

    def purposes(self) -> tuple[str, ...]:
        """The token set the pinned dashboard carries (``_tokens`` re-mints when it changes)."""
        if self.ctx.config.telegram.command_shortcuts:
            return PURPOSES + SHORTCUT_PURPOSES
        return PURPOSES

    def text(self, view: k.View) -> str:
        return classic.render(view, budget=DASHBOARD_MAX).text

    @staticmethod
    def digest(view: k.View, payload: dict[str, Any]) -> str:
        """The content hash: everything but the "Updated …" footer (which changes on every render)."""
        content = k.View(kind=view.kind, blocks=[b for b in view.blocks if b.name != "footer"], rows=view.rows)
        return sha256_text(classic.render(content).text + json.dumps(payload.get("reply_markup"), sort_keys=True))

    # ------------------------------------------------------------------ state
    def row(self) -> Any:
        return db.one(self.ctx.conn, "SELECT * FROM tg_views WHERE kind = 'dashboard' AND stale = 0"
                      " ORDER BY updated_at DESC LIMIT 1")  # fmt: skip

    def _tokens(self, message_id: int | None, state: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
        """Reuse the bound set while it has > 1 day left; else mint a fresh one (returns it and the new ones)."""
        now = self.ctx.clock.now()
        tokens = state.get("tokens") if isinstance(state.get("tokens"), dict) else None
        exp = state.get("tokens_exp")
        purposes = self.purposes()
        if tokens and exp and from_iso(str(exp)) - now > TOKEN_REFRESH and set(tokens) == set(purposes):
            return {str(p): str(t) for p, t in tokens.items()}, []
        fresh: dict[str, str] = {}
        with db.transaction(self.ctx.conn):
            for purpose in purposes:
                arg = {"hours": 1} if purpose == "quiet_set" else {}
                fresh[purpose] = self.ctx.approvals.mint_token(
                    purpose, chat_id=self.ctx.telegram_chat_id, message_id=message_id, expires_at=now + TOKEN_TTL,
                    arg=json.dumps(arg), single_use=False,
                )  # fmt: skip
        state["tokens_exp"] = to_iso(now + TOKEN_TTL)
        return fresh, list(fresh.values())

    def send_new(self) -> int | None:
        """Send a new dashboard (it is pinned once delivered)."""
        ctx = self.ctx
        if ctx.telegram_chat_id == 0:
            return None
        state: dict[str, Any] = {}
        tokens, minted = self._tokens(None, state)
        view = self.render(tokens)
        payload = self._payload(view, tokens)
        payload["disable_notification"] = True
        text_hash = self.digest(view, payload)
        record = {**ui_send.view_record(view), "state": {"tokens": tokens, "tokens_exp": state["tokens_exp"],
                                                         "hash": text_hash}}  # fmt: skip
        return ctx.outbox.enqueue("sendMessage", payload, dedupe_key=f"view:{new_token()}", bind_tokens=minted,
                                  view=record, thread_key="control", priority=3)  # fmt: skip

    def _send_queued(self) -> bool:
        return db.one(self.ctx.conn, "SELECT 1 FROM tg_outbox WHERE status = 'pending' AND method = 'sendMessage'"
                      " AND json_extract(view_json, '$.kind') = 'dashboard' LIMIT 1") is not None  # fmt: skip

    def _payload(self, view: k.View, tokens: dict[str, str], kill_armed: str | None = None) -> dict[str, Any]:
        markup = ui_send.keyboard(view, [[None] * len(r) for r in view.rows])
        return {"chat_id": self.ctx.telegram_chat_id, "text": self.text(view), "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True}, "reply_markup": markup}  # fmt: skip

    def tick(self, *, force: bool = False, kill_armed: str | None = None) -> bool:
        """Edit (or first send) the dashboard when due; True when something was queued."""
        ctx = self.ctx
        if ctx.telegram_chat_id == 0:
            return False
        current = self.row()
        if current is None:
            if self._send_queued():  # the first dashboard is still in the outbox: one dashboard, not three
                return False
            return self.send_new() is not None
        now = ctx.clock.now()
        interval = timedelta(seconds=ctx.config.telegram.dashboard.min_edit_interval_s)
        if not force and self._last_edit is not None and now - self._last_edit < interval:
            return False
        if not force and ctx.outbox.has_due_urgent():
            return False
        state = json.loads(current["state_json"] or "{}")
        tokens, _ = self._tokens(int(current["message_id"]), state)
        view = self.render(tokens, kill_armed=kill_armed)
        payload = self._payload(view, tokens)
        digest = self.digest(view, payload)
        if not force and digest == state.get("hash"):
            return False
        payload["message_id"] = int(current["message_id"])
        state.update({"tokens": tokens, "hash": digest})
        with db.transaction(ctx.conn):
            ctx.conn.execute("UPDATE tg_views SET state_json = ?, updated_at = ? WHERE message_id = ?",
                             (json.dumps(state, sort_keys=True), to_iso(now), current["message_id"]))  # fmt: skip
            ctx.outbox.enqueue("editMessageText", payload, dedupe_key=f"view:{new_token()}", priority=3,
                               thread_key="control")  # fmt: skip
        self._last_edit = now
        return True

    def is_pinned(self, message_id: int | None) -> bool:
        """Whether ``message_id`` is the current pinned dashboard (not a /status copy)."""
        current = self.row()
        return message_id is None or (current is not None and int(current["message_id"]) == message_id)

    def refresh_copy(self, message_id: int, *, kill_armed: str | None = None) -> None:
        """Re-render a /status copy in place (fresh tokens bound to that message)."""
        ui_send.edit(self.ctx, self.view(None, kill_armed=kill_armed, kind="status"), message_id=message_id)

    def arm_kill(self, message_id: int | None = None) -> str:
        """First Kill tap: a 10-s single-use second-tap token on the tapped message (D14): the pinned
        dashboard, or the /status copy the tap came from."""
        pinned = self.is_pinned(message_id)
        if pinned:
            current = self.row()
            if current is None:
                raise LookupError("no dashboard")
            message_id = int(current["message_id"])
        assert message_id is not None
        token = self.ctx.approvals.mint_token(
            "danger_second_tap", chat_id=self.ctx.telegram_chat_id, message_id=message_id,
            expires_at=self.ctx.clock.now() + KILL_SECOND_TAP, arg=json.dumps({"action": "kill"}),
        )  # fmt: skip
        if pinned:
            self.tick(force=True, kill_armed=token)
        else:
            self.refresh_copy(message_id, kill_armed=token)
        return token

    # ------------------------------------------------------------------ outbox hooks
    def _is_dashboard(self, message_id: Any) -> bool:
        if not isinstance(message_id, int):
            return False
        row = db.one(self.ctx.conn, "SELECT kind FROM tg_views WHERE message_id = ?", (message_id,))
        return row is not None and row["kind"] == "dashboard"

    def on_sent(self, row: OutboxRow, result: dict[str, Any]) -> None:
        """A new dashboard landed (its tg_views row was written by mark_sent): pin it silently and
        retire the older ones.

        ``pinChatMessage`` has no thread parameter (Bot API 10.3): the pin belongs to the topic the
        message is in (ADR-0020 addendum post-v1). So with Threaded mode on, only a dashboard whose
        send result is in 🚦 Control is pinned. One that landed elsewhere (the thread was not resolved)
        is retired and re-sent into Control, once per start; after that, or in flat mode, it is pinned
        where it is."""
        message_id = result.get("message_id")
        if row.method != "sendMessage" or not self._is_dashboard(message_id):
            return
        assert isinstance(message_id, int)
        raw_thread = result.get("message_thread_id")
        sent_thread = raw_thread if isinstance(raw_thread, int) else None
        from ossbot.gatekeeper.telegram.runtime import ui_of  # noqa: PLC0415 - runtime imports this module

        control = ui_of(self.ctx).topics.thread_id("control")  # None in flat mode
        if control is not None and sent_thread != control and not self._resent_to_control:
            self._resent_to_control = True
            with db.transaction(self.ctx.conn):
                self.ctx.conn.execute("UPDATE tg_views SET stale = 1 WHERE message_id = ?", (message_id,))
            log.warning("dashboard_resent_to_control: it landed in thread %s, not Control (%s)", sent_thread,
                        control, extra={"event": "dashboard_resent_to_control"})  # fmt: skip
            self.send_new()
            return
        with db.transaction(self.ctx.conn):
            self.ctx.conn.execute("UPDATE tg_views SET stale = 1 WHERE kind = 'dashboard' AND message_id != ?",
                                  (message_id,))  # fmt: skip
            view = db.one(self.ctx.conn, "SELECT state_json FROM tg_views WHERE message_id = ?", (message_id,))
            state = json.loads(view["state_json"] or "{}") if view is not None else {}
            state["sent_thread"] = sent_thread
            state["is_topic_message"] = result.get("is_topic_message") is True
            self.ctx.conn.execute("UPDATE tg_views SET state_json = ? WHERE message_id = ?",
                                  (json.dumps(state, sort_keys=True), message_id))  # fmt: skip
            self.ctx.outbox.enqueue("pinChatMessage", {"chat_id": self.ctx.telegram_chat_id, "message_id": message_id,
                                                       "disable_notification": True},
                                    dedupe_key=f"view:{new_token()}", priority=3)  # fmt: skip
        log.info("dashboard_pinned thread=%s", sent_thread if sent_thread is not None else "none",
                 extra={"event": "dashboard_pinned", "message_thread_id": sent_thread})  # fmt: skip

    def on_failed(self, row: OutboxRow, error: SendError) -> bool:
        mid = row.payload.get("message_id")
        if row.method == "pinChatMessage":
            log.warning("dashboard not pinned: %s", error, extra={"event": "dashboard_pin_failed"})
            return False
        if row.method == "editMessageText" and self._is_dashboard(mid) and "message to edit not found" in str(
                error).lower():  # fmt: skip
            with db.transaction(self.ctx.conn):
                self.ctx.conn.execute("UPDATE tg_views SET stale = 1 WHERE message_id = ?", (mid,))
            self.send_new()
            return True
        return False
