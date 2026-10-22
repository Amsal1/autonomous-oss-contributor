"""Card delivery for the three decision families (DESIGN §14.11.1, §14.11.12, §14.18.4.4; U3).

* Message 1 (the card) is sent to ✅ Reviews with one disabled ``⏳ delivering…`` button while its
  parts are pending; its message id binds every token (N12). The verbatim parts follow; Approve is
  minted and attached only when every part was delivered (``_maybe_arm``, one transaction).
* Each card and part is recorded in ``tg_views`` (kinds ``card``, ``body_part``, ``commit_msg``,
  ``proposal``, ``skill_card``, ``skill_part``) with its ``action_id``, so routing rule 5 and S12 can find
  them without reading message text.
* A card whose render withheld a verbatim scan hit or hit ``ViewTooLarge`` (both bugs) invalidates its
  approval ``render_failed`` and sends a fixed stub **without** ``approval_id`` (§14.18.1).
* Loudness (§14.11.13): classes 0-1 (claims, 👤 work) ring; others ring within
  ``telegram.loud_cards_per_hour``; autonomous proposals and cards re-sent on request are silent.
* ``recover`` (delta 8) heals every delivery crash window and, as step 3, renders S12/S5 for cards
  whose approval ended (≤ one housekeeping tick, AC 10).
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from telegram.constants import ParseMode

from ossbot.common.clock import to_iso
from ossbot.common.schemas import State
from ossbot.gatekeeper import auto_reissue, db
from ossbot.gatekeeper.approvals import ApprovalError, reissue_eligible
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.outbox import OutboxRow
from ossbot.gatekeeper.telegram import keyboards
from ossbot.gatekeeper.telegram.review import resolve, stale
from ossbot.gatekeeper.telegram.review.resolve import StaleShowCurrentArg
from ossbot.gatekeeper.telegram.runtime import ui_of
from ossbot.gatekeeper.telegram.ui import ids

log = logging.getLogger(__name__)
RETRY_TOKEN_TTL = timedelta(hours=24)
STALE_TOKEN_TTL = timedelta(hours=72)
CARD_PREFIX = "card:"
RETRY_PREFIX = "retry:"
# The three card families (§14.18.4.4): approval id prefix -> table.
DECISION_TABLES: dict[str, str] = {"a_": "approvals", "p_": "proposal_approvals", "s_": "skill_approvals"}
CARD_KIND = {"a_": "card", "p_": "proposal", "s_": "skill_card"}
LOUD_KINDS = frozenset({"issue_claim"})
ENDED = ("invalidated", "expired")


class CardPublisher:
    """Queues cards and retry notes through the outbox and finishes them as deliveries land."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ---------------------------------------------------------------- helpers
    def _decision_row(self, approval_id: str) -> Any:
        table = DECISION_TABLES.get(approval_id[:2])
        if table is None:
            return None
        return db.one(self.ctx.conn, f"SELECT * FROM {table} WHERE id = ?", (approval_id,))  # noqa: S608

    def meta(self, approval_id: str) -> dict[str, Any]:
        """``tg_views`` fields of a card: cid, rev, action_id (PR family), kind."""
        if approval_id.startswith("a_"):
            row = db.one(self.ctx.conn, "SELECT o.id, o.cid, o.kind, o.payload_json FROM approvals a JOIN"
                         " outbound_actions o ON o.id = a.action_id WHERE a.id = ?", (approval_id,))  # fmt: skip
            if row is None:
                return {}
            return {"cid": row["cid"], "rev": stale.rev_of(row["payload_json"]), "action_id": row["id"],
                    "kind": row["kind"]}  # fmt: skip
        row = self._decision_row(approval_id)
        if row is None:
            return {}
        out: dict[str, Any] = {"kind": "proposal" if approval_id.startswith("p_") else "skill"}
        if approval_id.startswith("p_"):
            out.update({"cid": row["cid"], "rev": int(row["version"])})
        else:
            out.update({"skill": row["skill"], "rev": int(row["version"])})
        return out

    def _loud(self, approval_id: str, meta: dict[str, Any]) -> bool:
        """§14.11.13: re-sends and autonomous proposals are silent; claims and 👤 work ring; the rest ring
        within ``loud_cards_per_hour``."""
        conn = self.ctx.conn
        if approval_id.startswith("a_"):
            again = db.one(conn, "SELECT 1 FROM approvals WHERE action_id = ? AND id != ?",
                           (meta.get("action_id"), approval_id))  # fmt: skip
            if again is not None:
                return False
        elif approval_id.startswith("p_"):
            again = db.one(conn, "SELECT 1 FROM proposal_approvals WHERE cid = ? AND id != ?", (meta.get("cid"),
                                                                                              approval_id))  # fmt: skip
            if again is not None:
                return False
        cid = meta.get("cid")
        user = cid is not None and db.one(conn, "SELECT 1 FROM contributions WHERE id = ? AND source = 'user'",
                                          (cid,)) is not None  # fmt: skip
        if user or meta.get("kind") in LOUD_KINDS:
            return True
        if approval_id.startswith("p_"):
            return False  # autonomous proposals are never loud
        since = to_iso(self.ctx.clock.now() - timedelta(hours=1))
        recent = db.one(conn, "SELECT COUNT(*) FROM tg_outbox WHERE dedupe_key LIKE 'card:%' AND priority = 1"
                        " AND next_attempt_at >= ?", (since,))  # fmt: skip
        return int(recent[0] if recent is not None else 0) < self.ctx.config.telegram.loud_cards_per_hour

    def _chain_to(self, approval_id: str, meta: dict[str, Any]) -> int | None:
        """Later proposal versions and the PR preview card reply to the latest proposal card of the cid."""
        cid = meta.get("cid")
        if cid is None or not (approval_id.startswith("p_") or meta.get("kind") == "open_pr"):
            return None
        row = db.one(self.ctx.conn, "SELECT tg_message_id FROM proposal_approvals WHERE cid = ? AND id != ? AND"
                     " tg_message_id IS NOT NULL ORDER BY version DESC, created_at DESC LIMIT 1",
                     (cid, approval_id))  # fmt: skip
        if row is not None:
            return int(row["tg_message_id"])
        if approval_id.startswith("p_"):
            return self._working_on_it(str(cid))
        return None

    def _working_on_it(self, cid: str) -> int | None:
        """A 👤 v1 card replies to the ``Working on it`` message when that is in ✅ Reviews (replies can't
        cross topics; in flat mode everything is one chat) (§14.11.15 Delivery)."""
        view = db.one(self.ctx.conn, "SELECT message_id, thread_id FROM tg_views WHERE cid = ? AND kind ="
                      " 'work_request' ORDER BY message_id DESC LIMIT 1", (cid,))  # fmt: skip
        if view is None:
            return None
        topics = ui_of(self.ctx).topics
        if topics.mode == "flat" or view["thread_id"] == topics.thread_id("reviews"):
            return int(view["message_id"])
        return None

    def _invalidate_render_failed(self, approval_id: str) -> None:
        svc: Any = self.ctx.decisions.for_id(approval_id)
        with db.transaction(self.ctx.conn):
            row = self._decision_row(approval_id)
            if row is not None and row["status"] in ("pending", "armed", "confirmed"):
                svc._mark_invalid(row, "render_failed", "system")

    # ---------------------------------------------------------------- send
    def publish(self, approval_id: str) -> None:
        """Message 1 of an ``a_``, ``p_`` or ``s_`` card (resolved through ``ctx.decisions.for_id``)."""
        chat = self.ctx.telegram_chat_id
        if chat == 0:
            log.warning("telegram not configured; card %s not sent", approval_id, extra={"event": "card_dropped"})
            return
        svc = self.ctx.decisions.for_id(approval_id)
        rendered = svc.card_view(approval_id)
        meta = self.meta(approval_id)
        if getattr(rendered, "withheld", False) or getattr(rendered, "too_large", False):
            log.error("card %s not renderable (%s)", approval_id, "withheld" if rendered.withheld else "too large",
                      extra={"event": "card_render_failed"})  # fmt: skip
            self._invalidate_render_failed(approval_id)
            self.ctx.outbox.enqueue("sendMessage", {"chat_id": chat, "text": rendered.card, "parse_mode": "HTML"},
                                    dedupe_key=f"{CARD_PREFIX}{approval_id}", thread_key="reviews")  # fmt: skip
            return
        payload: dict[str, Any] = {"chat_id": chat, "text": rendered.card, "parse_mode": ParseMode.HTML,
                                   "link_preview_options": {"is_disabled": True}}  # fmt: skip
        parts = len(rendered.parts)
        if parts:
            payload["reply_markup"] = keyboards.disabled_markup(f"⏳ delivering {parts} part{'s' * (parts > 1)}…")
        chain = self._chain_to(approval_id, meta)
        if chain is not None:  # one candidate's history is one reply chain (§14.11.15 Delivery)
            payload["reply_parameters"] = {"message_id": chain, "allow_sending_without_reply": True}
        loud = self._loud(approval_id, meta)
        payload["disable_notification"] = not loud
        view = {"kind": CARD_KIND[approval_id[:2]], "approval_id": approval_id, "cid": meta.get("cid"),
                "rev": meta.get("rev"), "action_id": meta.get("action_id"),
                "state": {"skill": meta["skill"]} if "skill" in meta else {}}  # fmt: skip
        self.ctx.outbox.enqueue("sendMessage", payload, dedupe_key=f"{CARD_PREFIX}{approval_id}",
                                approval_id=approval_id, view=view, thread_key="reviews",
                                priority=1 if loud else 2)  # fmt: skip

    def retry_note(self, action_id: str, text: str) -> None:
        chat = self.ctx.telegram_chat_id
        if chat == 0:
            self.ctx.notify(text)
            return
        self.ctx.outbox.enqueue(
            "sendMessage",
            {"chat_id": chat, "text": ids.autocode(text), "parse_mode": ParseMode.HTML},
            dedupe_key=f"{RETRY_PREFIX}{action_id}",
            thread_key="reviews",
        )

    async def on_sent(self, row: OutboxRow, result: dict[str, Any]) -> None:
        key = row.dedupe_key or ""
        if key.startswith(CARD_PREFIX) and row.approval_id is not None:
            self._card_sent(row.approval_id, int(result["message_id"]))
        elif key.startswith(RETRY_PREFIX):
            self._retry_sent(key[len(RETRY_PREFIX) :], int(result["message_id"]))
        elif row.approval_id is not None and row.part_index is not None:
            self._maybe_arm(row.approval_id)

    def _card_sent(self, approval_id: str, message_id: int) -> None:
        """Message id + every part enqueue in one transaction (delta 8); a card without parts
        (proposals) is armed right away."""
        svc = self.ctx.decisions.for_id(approval_id)
        meta = self.meta(approval_id)
        with db.transaction(self.ctx.conn):
            svc.set_card_message(approval_id, message_id)
            rendered = svc.card_view(approval_id)
            kinds = list(getattr(rendered, "part_kinds", []) or [])
            for index, part in enumerate(rendered.parts):
                kind = kinds[index] if index < len(kinds) else (
                    "skill_part" if approval_id.startswith("s_") else "body_part")  # fmt: skip
                self.ctx.outbox.enqueue(
                    "sendMessage",
                    {"chat_id": self.ctx.telegram_chat_id, "text": part, "parse_mode": ParseMode.HTML,
                     "disable_notification": True, "reply_parameters": {"message_id": message_id,
                                                                         "allow_sending_without_reply": True}},
                    dedupe_key=f"part:{approval_id}:{index}",
                    approval_id=approval_id,
                    part_index=index,
                    view={"kind": kind, "approval_id": approval_id, "cid": meta.get("cid"), "rev": meta.get("rev"),
                          "action_id": meta.get("action_id"), "page": index + 1},
                    thread_key="reviews",
                )  # fmt: skip
        if svc.part_count(approval_id) == 0:
            self._maybe_arm(approval_id)

    def _maybe_arm(self, approval_id: str) -> None:
        """Card tokens + the ``arm:<aid>`` edit in one transaction (delta 8): "no arm row" means "no
        card tokens", so Approve is minted exactly once."""
        svc = self.ctx.decisions.for_id(approval_id)
        row = self._decision_row(approval_id)
        if row is None or row["tg_message_id"] is None or row["status"] != "pending":
            return
        if svc.part_count(approval_id) and row["body_parts_delivered"] < row["body_parts_total"]:
            return
        if not self.ctx.approvals.system_running():
            # No decision token is minted while paused or killed (ADR-0008 addendum): recover() step 2
            # arms the card on the first tick after resume.
            log.info("card %s not armed while paused or killed", approval_id, extra={"event": "card_tokens_deferred"})
            return
        try:
            with db.transaction(self.ctx.conn):
                tokens = svc.issue_card_tokens(approval_id)
                self.ctx.outbox.enqueue(
                    "editMessageReplyMarkup",
                    {
                        "chat_id": row["tg_chat_id"],
                        "message_id": row["tg_message_id"],
                        "reply_markup": keyboards.to_json(svc.card_rows(approval_id, tokens)),
                    },
                    dedupe_key=f"arm:{approval_id}",
                )
        except ApprovalError as exc:
            log.warning("card tokens not issued: %s", exc.code, extra={"event": "card_tokens_refused"})

    def render_s1(self, approval_id: str, armed_at: str, tokens: dict[str, str]) -> None:
        """Delta 2: a timed-out Confirm goes back to the S1 card with fresh tokens. Called inside the
        service's disarm transaction; the key carries ``armed_at`` (it can repeat per approval, BA-3)."""
        svc = self.ctx.decisions.for_id(approval_id)
        row = self._decision_row(approval_id)
        if row is None or row["tg_message_id"] is None:
            return
        text = svc.card_view(approval_id).card + "\n↩️ Confirm timed out — tap Approve again"
        self.ctx.outbox.enqueue(
            "editMessageText",
            {"chat_id": row["tg_chat_id"], "message_id": row["tg_message_id"], "text": text,
             "parse_mode": ParseMode.HTML, "link_preview_options": {"is_disabled": True},
             "reply_markup": keyboards.to_json(svc.card_rows(approval_id, tokens))},
            dedupe_key=f"disarm:{approval_id}:{armed_at}", thread_key="reviews",
        )  # fmt: skip

    def render_current(self, approval_id: str, occurrence: str) -> None:
        """Re-render an approval's real state after a race (§14.11.5): S1 when pending (fresh rotated
        tokens), S12 when it ended. Keys carry the occurrence (``s1:``/``stale:<aid>:<comment_id>``)."""
        row = self._decision_row(approval_id)
        if row is None or row["tg_message_id"] is None:
            return
        if row["status"] == "pending":
            svc = self.ctx.decisions.for_id(approval_id)
            tokens = svc.rotate_card_tokens(approval_id) if hasattr(svc, "rotate_card_tokens") else {}
            self.ctx.outbox.enqueue(
                "editMessageText",
                {"chat_id": row["tg_chat_id"], "message_id": row["tg_message_id"],
                 "text": svc.card_view(approval_id).card, "parse_mode": ParseMode.HTML,
                 "reply_markup": keyboards.to_json(svc.card_rows(approval_id, tokens))},
                dedupe_key=f"s1:{approval_id}:{occurrence}", thread_key="reviews",
            )  # fmt: skip
        elif row["status"] in ENDED:
            self.render_stale(approval_id, key=f"stale:{approval_id}:{occurrence}")

    # ---------------------------------------------------------------- S12
    def _retry_eligible(self, meta: dict[str, Any]) -> bool:
        """The action of a Retry row is still waiting (the tap itself refuses while paused or killed)."""
        action_id = meta.get("action_id")
        if action_id is None:
            return False
        row = db.one(self.ctx.conn, "SELECT o.kind, o.status, c.state FROM outbound_actions o JOIN contributions c"
                     " ON c.id = o.cid WHERE o.id = ?", (action_id,))  # fmt: skip
        if row is None:
            return False
        try:
            return reissue_eligible(row["kind"], row["status"], State(row["state"]))
        except ValueError:
            return False

    def _forward(self, approval_id: str, row: Any, meta: dict[str, Any]) -> tuple[stale.Forward, str | None]:
        """§14.11.12 forward rule: ``⬇️ Show current`` when an open card exists or a re-send is owed
        (``auto_reissue``, deferred while paused or killed); ``🔁 Retry`` for the explicit-retry rows
        while the action is eligible; otherwise no button and one reason line."""
        conn = self.ctx.conn
        cid, skill = meta.get("cid"), meta.get("skill")
        gate = 1 if approval_id.startswith("p_") else None
        status, reason = str(row["status"]), row["invalidated_reason"]
        if (cid or skill) and resolve.open_card_exists(conn, StaleShowCurrentArg.model_validate(
                stale.show_arg(cid, skill, gate))):  # fmt: skip
            return "show_current", None
        now = self.ctx.clock.now()
        floor = now + timedelta(seconds=self.ctx.config.policy.limits.confirm_ttl_seconds)
        if skill is not None:
            staged = self.ctx.decisions.skills.source.reissuable(conn, str(skill)) is not None
            if staged and auto_reissue.owed_for(conn, now, floor, skill=str(skill)):
                return "show_current", None
            return "none", stale.SKILL_REOPEN if staged else stale.nothing_line(skill=str(skill))
        if cid is None:
            return "none", None
        if auto_reissue.owed_for(conn, now, floor, cid=str(cid)):
            return "show_current", None
        held = auto_reissue.held(conn, str(cid))
        if gate is None and not held and self._retry_eligible(meta):
            return ("retry", None) if stale.is_retry_row(status, reason) else ("none", stale.REOPEN)
        state = resolve.state_of(conn, str(cid))
        if gate == 1 and not held and state == "PROPOSED":
            return "none", stale.REOPEN
        return "none", stale.nothing_line(state=state, gate=gate, held=held)

    def render_stale(self, approval_id: str, *, key: str | None = None) -> bool:
        """S12/S5 for an ended approval: the card edit (dedupe ``stale:<aid>``) plus part stubs."""
        row = self._decision_row(approval_id)
        if row is None or row["tg_message_id"] is None or row["status"] not in ENDED:
            return False
        meta = self.meta(approval_id)
        svc = self.ctx.decisions.for_id(approval_id)
        try:
            card_html = svc.card_view(approval_id).card
        except (ApprovalError, KeyError):
            card_html = ids.autocode(f"{meta.get('cid') or meta.get('skill') or approval_id}")
        reason = row["invalidated_reason"]
        forward, reason_line = self._forward(approval_id, row, meta)
        rendered = stale.render(
            card_html=card_html, status=str(row["status"]), reason=reason, cid=meta.get("cid"),
            rev=meta.get("rev"), action_id=meta.get("action_id"), skill=meta.get("skill"),
            gate=1 if approval_id.startswith("p_") else None, forward=forward, reason_line=reason_line,
        )  # fmt: skip
        chat, mid = int(row["tg_chat_id"]), int(row["tg_message_id"])
        disabled = {"text": stale.disabled_label(approval_id), "disabled": {}}
        buttons = [disabled]
        with db.transaction(self.ctx.conn):
            for label, purpose, arg in rendered.buttons:
                token = self.ctx.approvals.mint_token(
                    purpose, chat_id=chat, message_id=mid, expires_at=self.ctx.clock.now() + STALE_TOKEN_TTL,
                    cid=meta.get("cid"), arg=json.dumps(arg, sort_keys=True), single_use=purpose == "stale_retry",
                )  # fmt: skip
                buttons.append({"text": label, "callback_data": keyboards.callback_data(token)})
            self.ctx.outbox.enqueue(
                "editMessageText",
                {"chat_id": chat, "message_id": mid, "text": rendered.text,
                 "parse_mode": ParseMode.HTML, "link_preview_options": {"is_disabled": True},
                 "reply_markup": {"inline_keyboard": [buttons]}},
                dedupe_key=key or f"stale:{approval_id}", thread_key="reviews",
            )  # fmt: skip
            for part in stale.stale_views(self.ctx.conn, approval_id):
                self.ctx.outbox.enqueue(
                    "editMessageText",
                    {"chat_id": chat, "message_id": int(part["message_id"]),
                     "text": stale.part_stub(meta.get("cid") or meta.get("skill"), meta.get("rev")),
                     "parse_mode": ParseMode.HTML},
                    dedupe_key=f"stub:{int(part['message_id'])}:{approval_id}",
                )  # fmt: skip
                self.ctx.conn.execute("UPDATE tg_views SET stale = 1 WHERE message_id = ?", (part["message_id"],))
            self.ctx.conn.execute("UPDATE tg_views SET stale = 1 WHERE message_id = ?", (mid,))
            if meta.get("action_id") is not None and reason in {"new_package_rev", "entered_implementing",
                                                                "changes_requested", "fix_round_failed"}:  # fmt: skip
                stale.mark_action_views_stale(self.ctx.conn, str(meta["action_id"]))
        return True

    def stale_sweep(self) -> int:
        """Step 3 of ``recover``: every ended approval whose card was sent and not yet staled."""
        n = 0
        for table in DECISION_TABLES.values():
            rows = self.ctx.conn.execute(
                f"SELECT d.id FROM {table} d WHERE d.status IN ('invalidated','expired') AND d.tg_message_id IS NOT"  # noqa: S608
                " NULL AND NOT EXISTS (SELECT 1 FROM tg_outbox o WHERE o.dedupe_key = 'stale:' || d.id)"
            ).fetchall()
            for r in rows:
                if self.render_stale(str(r["id"])):
                    n += 1
        return n

    def recover(self) -> None:
        """Delta 8 (§14.18.4.4): heal every crash window of card delivery. Run once before polling
        starts and on every housekeeping tick (``ctx.card_recover``). Never counts parts. Step 3
        renders S12 for ended approvals (AC 10). First, owed cards are re-sent (``AppContext.reissue_due``:
        a start-time snapshot change, kill -> resume, a deferred reload or toggle), so the stale sweep
        below already sees the fresh card and offers ``⬇️ Show current``."""
        conn = self.ctx.conn
        try:
            self.ctx.reissue_due()
        except Exception:
            log.exception("auto re-send failed", extra={"event": "auto_reissue_error"})
        for table in DECISION_TABLES.values():
            # 0. approval created, card never enqueued (a failed on_approval_created after a commit)
            for r in conn.execute(
                f"SELECT d.id FROM {table} d WHERE d.status = 'pending' AND d.tg_message_id IS NULL"  # noqa: S608
                " AND NOT EXISTS (SELECT 1 FROM tg_outbox o WHERE o.dedupe_key = 'card:' || d.id)"
            ).fetchall():
                self.ctx.on_approval_created(str(r["id"]))
            # 1. card sent, message id lost
            for r in conn.execute(
                f"SELECT d.id, o.result_message_id FROM {table} d JOIN tg_outbox o ON o.dedupe_key = 'card:' || d.id"  # noqa: S608
                " WHERE d.status = 'pending' AND d.tg_message_id IS NULL AND o.status = 'sent'"
            ).fetchall():
                if r["result_message_id"] is None:
                    log.warning("card %s sent before result_message_id existed; reach it through /queue", r["id"],
                                extra={"event": "card_recover_skipped"})  # fmt: skip
                    continue
                self._card_sent(str(r["id"]), int(r["result_message_id"]))
            # 2. parts delivered (or none), never armed
            parts = "" if table == "proposal_approvals" else " AND d.body_parts_delivered >= d.body_parts_total"
            for r in conn.execute(
                f"SELECT d.id FROM {table} d WHERE d.status = 'pending' AND d.tg_message_id IS NOT NULL{parts}"  # noqa: S608
                " AND NOT EXISTS (SELECT 1 FROM tg_outbox o WHERE o.dedupe_key = 'arm:' || d.id)"
            ).fetchall():
                self._maybe_arm(str(r["id"]))
        self.stale_sweep()

    def _retry_sent(self, action_id: str, message_id: int) -> None:
        chat = self.ctx.telegram_chat_id
        token = self.ctx.approvals.mint_token(
            "retry", chat_id=chat, message_id=message_id, expires_at=self.ctx.clock.now() + RETRY_TOKEN_TTL,
            arg=action_id,
        )  # fmt: skip
        self.ctx.outbox.enqueue(
            "editMessageReplyMarkup",
            {"chat_id": chat, "message_id": message_id,
             "reply_markup": keyboards.to_json(keyboards.single_row("🔁 Retry", token))},
            dedupe_key=f"retry-arm:{action_id}",
        )  # fmt: skip
