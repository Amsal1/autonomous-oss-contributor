"""The three deterministic decision services and their common shape (DESIGN §14.18.4.2, delta 4/9).

* ``ApprovalService`` (``a_`` ids, outbound actions; ``approvals.py``), ``ProposalApprovals``
  (``p_``, CR-004 gate 1) and ``SkillApprovals`` (``s_``, CR-002 skill activation) implement
  ``DecisionService``. ``DecisionServices.for_id`` dispatches on the id prefix, so a token can never
  be resolved by the wrong service; each service also checks the purposes it accepts.
* Kill and reload fan out over ``DecisionServices.all()`` (delta 9); the dry-run toggle reaches
  ``ApprovalService`` only (proposal and skill bindings carry no mode).
* ``TableDecisions`` is the shared two-tap core of the proposal and skill services: single-use,
  message-bound tokens minted through ``ApprovalService.mint_token``/``use_token`` (the reviewed
  token code), the binding recomputed at Approve, at Confirm and in ``consume`` right before the
  effect, refusals while the system may not act. Neither calls ``ApprovalService.confirm_text``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from ossbot.common.clock import Clock, from_iso, to_iso
from ossbot.common.hashing import canonical_json
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import (
    OPEN_STATUSES,
    ApprovalError,
    ApprovalService,
    ArmResult,
    Invalidated,
    OnDisarmed,
    Superseded,
    binding_hash,
)
from ossbot.gatekeeper.audit import AuditLog
from ossbot.gatekeeper.telegram import keyboards
from ossbot.gatekeeper.telegram.cards import CardMessages
from ossbot.gatekeeper.telegram.escape import b, esc

if TYPE_CHECKING:
    from ossbot.gatekeeper.proposal_approvals import ProposalApprovals
    from ossbot.gatekeeper.skill_approvals import SkillApprovals

# None = the system may act now; otherwise the refusal reason (paused, killed, draining, down).
Refusal = Callable[[], str | None]
Renderer = Callable[[sqlite3.Row], CardMessages]


class DecisionService(Protocol):
    id_prefix: str
    on_disarmed: OnDisarmed  # CardPublisher.render_s1, called inside disarm_expired's transaction

    def invalidate_all_open(self, reason: str, actor: str = "system") -> list[Invalidated]: ...
    def invalidate_armed_confirmed(self, reason: str, actor: str = "system") -> list[Invalidated]: ...
    def expire_due(self) -> list[str]: ...
    def disarm_expired(self) -> list[str]: ...
    def set_card_message(self, approval_id: str, tg_message_id: int) -> None: ...
    def issue_card_tokens(self, approval_id: str) -> dict[str, str]: ...
    def card_view(self, approval_id: str) -> CardMessages: ...
    def card_rows(self, approval_id: str, tokens: dict[str, str]) -> keyboards.Rows: ...
    def part_count(self, approval_id: str) -> int: ...


@dataclass(frozen=True)
class DecisionServices:
    """``AppContext.decisions``."""

    approvals: ApprovalService
    proposals: ProposalApprovals
    skills: SkillApprovals

    def all(self) -> tuple[DecisionService, DecisionService, DecisionService]:
        return (self.approvals, self.proposals, self.skills)

    def for_id(self, approval_id: str) -> DecisionService:
        for svc in self.all():
            if approval_id.startswith(f"{svc.id_prefix}_"):
                return svc
        raise ApprovalError("unknown_approval", approval_id)

    def invalidate_all_open(self, reason: str, actor: str = "system") -> list[Invalidated]:
        """Reload (both branches): all three services, inside the caller's transaction."""
        return [i for svc in self.all() for i in svc.invalidate_all_open(reason, actor)]

    def invalidate_armed_confirmed(self, reason: str, actor: str = "system") -> list[Invalidated]:
        """Kill (the four paths in control.py): all three services, inside the caller's transaction."""
        return [i for svc in self.all() for i in svc.invalidate_armed_confirmed(reason, actor)]


def _default_renderer(title: str) -> Renderer:
    def render(row: sqlite3.Row) -> CardMessages:
        subject = row["cid"] if "cid" in row.keys() else row["skill"]  # noqa: SIM118 - Row's `in` tests values
        return CardMessages(card=f"{b(title)} · {esc(str(subject))} · v{int(row['version'])}")

    return render


class TableDecisions:
    """Shared two-tap core over one ``*_approvals`` table (pending -> armed -> confirmed -> consumed)."""

    table: ClassVar[str]
    id_prefix: str  # instance-visible: the DecisionService protocol member
    kind: ClassVar[str]  # the binding's "kind"
    approve_purpose: ClassVar[str]
    confirm_purpose: ClassVar[str]
    cancel_purpose: ClassVar[str]
    reject_purposes: ClassVar[tuple[str, ...]] = ()
    view_purposes: ClassVar[tuple[str, ...]] = ()
    has_parts: ClassVar[bool] = False
    card_title: ClassVar[str] = "Decision"

    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        audit: AuditLog,
        tokens: ApprovalService,
        ttl_hours: Callable[[], int],
        confirm_ttl_seconds: Callable[[], int],
        refusal: Refusal,
        renderer: Renderer | None = None,
        chat_id: Callable[[], int] | None = None,
    ) -> None:
        self.chat_id: Callable[[], int] = chat_id or (lambda: 0)  # the owner's chat, for re-sent cards
        self.conn = conn
        self.clock = clock
        self.audit = audit
        self.tokens = tokens  # mint_token / use_token / check_token / _kill_tokens: the reviewed token code
        self.ttl_hours = ttl_hours
        self.confirm_ttl_seconds = confirm_ttl_seconds
        self.refusal = refusal
        self.renderer: Renderer = renderer or _default_renderer(self.card_title)
        self.on_disarmed: OnDisarmed = lambda approval_id, armed_at, tokens: None

    # ------------------------------------------------------------- subclass hooks
    def fields(self, row: sqlite3.Row) -> dict[str, Any]:
        """The bound fields recomputed now from the subject (proposal content, staged skill)."""
        raise NotImplementedError

    def subject(self, row: sqlite3.Row) -> dict[str, Any]:
        raise NotImplementedError

    def cid_of(self, row: sqlite3.Row) -> str | None:
        return None

    def confirm_text(self, binding: Mapping[str, Any]) -> str:
        raise NotImplementedError

    # ---------------------------------------------------------------------- helpers
    def _now(self) -> datetime:
        return self.clock.now()

    def _row(self, approval_id: str) -> sqlite3.Row:
        if not approval_id.startswith(f"{self.id_prefix}_"):
            raise ApprovalError("unknown_approval", approval_id)
        row = db.one(self.conn, f"SELECT * FROM {self.table} WHERE id = ?", (approval_id,))  # noqa: S608
        if row is None:
            raise ApprovalError("unknown_approval", approval_id)
        return row

    def binding(self, row: sqlite3.Row) -> dict[str, Any]:
        return {"v": 1, "kind": self.kind, **self.subject(row), **self.fields(row)}

    def _token_row(self, token: str, purposes: tuple[str, ...], chat_id: int, message_id: int) -> sqlite3.Row:
        tok = self.tokens.use_token(token, purposes, chat_id=chat_id, message_id=message_id)
        if tok["approval_id"] is None:
            raise ApprovalError("token_purpose", str(tok["purpose"]))
        return self._row(str(tok["approval_id"]))

    def _mark_invalid(self, row: sqlite3.Row, reason: str, actor: str) -> None:
        self.conn.execute(
            f"UPDATE {self.table} SET status = 'invalidated', invalidated_reason = ? WHERE id = ?",  # noqa: S608
            (reason, row["id"]),
        )
        self.tokens._kill_tokens([row["id"]])
        self.audit.append(actor, "approval_invalidated", {"approval_id": row["id"], "reason": reason}, self.cid_of(row))

    def _recheck(self, row: sqlite3.Row, actor: str) -> dict[str, Any]:
        try:
            binding = self.binding(row)
        except (ApprovalError, KeyError, ValueError) as exc:
            reason = f"binding_unavailable:{getattr(exc, 'code', type(exc).__name__)}"
            self._mark_invalid(row, reason, actor)
            raise Superseded("superseded", reason) from exc
        if binding_hash(binding) != row["binding_hash"]:
            self._mark_invalid(row, "binding_changed", actor)
            raise Superseded("superseded", "content changed since this card; review the new card")
        return binding

    def _check_live(self, row: sqlite3.Row, wanted: str) -> None:
        if row["status"] != wanted:
            raise ApprovalError("wrong_status", f"{row['status']} (expected {wanted})")
        if from_iso(row["expires_at"]) <= self._now():
            raise ApprovalError("approval_expired")

    def _refuse_if_blocked(self, code: str) -> None:
        reason = self.refusal()
        if reason is not None:
            raise ApprovalError(code, reason)

    def _expiry(self, expires_at: datetime | None) -> datetime:
        now = self._now()
        if expires_at is not None and expires_at <= now:
            raise ApprovalError("bad_expiry", to_iso(expires_at))
        return expires_at or now + timedelta(hours=self.ttl_hours())

    def _insert(self, approval_id: str, columns: Mapping[str, Any], binding: dict[str, Any], tg_chat_id: int,
                expires_at: datetime, actor: str, cid: str | None) -> None:  # fmt: skip
        values: dict[str, Any] = {
            "id": approval_id, **columns, "binding_json": canonical_json(binding).decode("utf-8"),
            "binding_hash": binding_hash(binding), "status": "pending", "tg_chat_id": tg_chat_id,
            "created_at": to_iso(self._now()), "expires_at": to_iso(expires_at),
        }  # fmt: skip
        names = ", ".join(values)
        marks = ", ".join("?" * len(values))
        self.conn.execute(f"INSERT INTO {self.table} ({names}) VALUES ({marks})", tuple(values.values()))  # noqa: S608
        self.audit.append(
            actor, "approval_created", {"approval_id": approval_id, "binding_hash": binding_hash(binding)}, cid
        )

    def _invalidate(self, where: str, params: tuple[Any, ...], reason: str, actor: str) -> list[Invalidated]:
        with db.transaction(self.conn):
            rows = self.conn.execute(f"SELECT * FROM {self.table} WHERE {where}", params).fetchall()  # noqa: S608
            out = [Invalidated(str(r["id"]), None, self.cid_of(r)) for r in rows]
            for r in rows:
                self.conn.execute(
                    f"UPDATE {self.table} SET status = 'invalidated', invalidated_reason = ? WHERE id = ?",  # noqa: S608
                    (reason, r["id"]),
                )
                self.audit.append(actor, "approval_invalidated", {"approval_id": r["id"], "reason": reason},
                                  self.cid_of(r))  # fmt: skip
            self.tokens._kill_tokens([i.approval_id for i in out])
        return out

    # ------------------------------------------------------------------ protocol
    def invalidate_all_open(self, reason: str, actor: str = "system") -> list[Invalidated]:
        return self._invalidate("status IN ('pending','armed','confirmed')", (), reason, actor)

    def invalidate_armed_confirmed(self, reason: str, actor: str = "system") -> list[Invalidated]:
        return self._invalidate("status IN ('armed','confirmed')", (), reason, actor)

    def expire_due(self) -> list[str]:
        now = to_iso(self._now())
        with db.transaction(self.conn):
            rows = self.conn.execute(
                f"SELECT * FROM {self.table} WHERE status IN ('pending','armed','confirmed') AND expires_at <= ?",  # noqa: S608
                (now,),
            ).fetchall()
            for r in rows:
                self.conn.execute(f"UPDATE {self.table} SET status = 'expired' WHERE id = ?", (r["id"],))  # noqa: S608
                self.audit.append("system", "approval_expired", {"approval_id": r["id"]}, self.cid_of(r))
            self.tokens._kill_tokens([str(r["id"]) for r in rows])
        return [str(r["id"]) for r in rows]

    def disarm_expired(self) -> list[str]:
        cutoff = to_iso(self._now() - timedelta(seconds=self.confirm_ttl_seconds()))
        done: list[str] = []
        with db.transaction(self.conn):
            rows = self.conn.execute(
                f"SELECT * FROM {self.table} WHERE status = 'armed' AND armed_at <= ?",  # noqa: S608
                (cutoff,),
            ).fetchall()
            for r in rows:
                self.conn.execute(
                    f"UPDATE {self.table} SET status = 'pending', armed_at = NULL WHERE id = ?",  # noqa: S608
                    (r["id"],),
                )
                tokens = self.rotate_card_tokens(str(r["id"]))
                self.audit.append("system", "approval_disarmed",
                                  {"approval_id": r["id"], "reason": "confirm_expired"}, self.cid_of(r))  # fmt: skip
                self.on_disarmed(str(r["id"]), str(r["armed_at"]), tokens)
                done.append(str(r["id"]))
        return done

    def set_card_message(self, approval_id: str, tg_message_id: int) -> None:
        with db.transaction(self.conn):
            self._row(approval_id)
            self.conn.execute(
                f"UPDATE {self.table} SET tg_message_id = ? WHERE id = ?",  # noqa: S608
                (tg_message_id, approval_id),
            )

    def card_purposes(self) -> tuple[str, ...]:
        return (self.approve_purpose, *self.reject_purposes, *self.view_purposes)

    def issue_card_tokens(self, approval_id: str) -> dict[str, str]:
        """Minted after delivery and attached by editMessageReplyMarkup (U3), like PR cards."""
        with db.transaction(self.conn):
            row = self._row(approval_id)
            if row["status"] != "pending":
                raise ApprovalError("not_pending", row["status"])
            if row["tg_message_id"] is None:
                raise ApprovalError("no_message")
            if self.has_parts and row["body_parts_delivered"] < row["body_parts_total"]:
                raise ApprovalError("body_not_delivered", f"{row['body_parts_delivered']}/{row['body_parts_total']}")
            expires = from_iso(row["expires_at"])
            return {
                p: self.tokens.mint_token(
                    p, chat_id=row["tg_chat_id"], message_id=row["tg_message_id"], expires_at=expires,
                    approval_id=approval_id, cid=self.cid_of(row), single_use=p not in self.view_purposes,
                )
                for p in self.card_purposes()
            }  # fmt: skip

    def rotate_card_tokens(self, approval_id: str) -> dict[str, str]:
        with db.transaction(self.conn):
            self.tokens._kill_tokens([approval_id])
            return self.issue_card_tokens(approval_id)

    def card_view(self, approval_id: str) -> CardMessages:
        return self.renderer(self._row(approval_id))

    def card_rows(self, approval_id: str, tokens: dict[str, str]) -> keyboards.Rows:
        row: list[keyboards.Button] = [("✅ Approve", tokens[self.approve_purpose])]
        row += [("❌ Reject", tokens[p]) for p in self.reject_purposes[:1]]
        return [row]

    def part_count(self, approval_id: str) -> int:
        row = self._row(approval_id)
        return int(row["body_parts_total"]) if self.has_parts else 0

    # ------------------------------------------------------------------ decisions
    def approve(self, token: str, *, tg_user_id: int, chat_id: int, message_id: int) -> ArmResult:
        actor = f"tg:{tg_user_id}"
        result: ArmResult | None = None
        error: ApprovalError | None = None
        self._refuse_if_blocked("approve_refused")  # before the token is used: the card stays tappable
        with db.transaction(self.conn):
            row = self._token_row(token, (self.approve_purpose,), chat_id, message_id)
            try:
                self._check_live(row, "pending")
                binding = self._recheck(row, actor)
            except ApprovalError as exc:
                error = exc
            else:
                now = self._now()
                expiry = now + timedelta(seconds=self.confirm_ttl_seconds())
                self.conn.execute(
                    f"UPDATE {self.table} SET status = 'armed', armed_at = ? WHERE id = ?",  # noqa: S608
                    (to_iso(now), row["id"]),
                )
                mint = {"chat_id": chat_id, "message_id": message_id, "expires_at": expiry,
                        "approval_id": row["id"], "cid": self.cid_of(row)}  # fmt: skip
                confirm = self.tokens.mint_token(self.confirm_purpose, **mint)
                cancel = self.tokens.mint_token(self.cancel_purpose, **mint)
                self.audit.append(actor, "approval_armed", {"approval_id": row["id"]}, self.cid_of(row))
                result = ArmResult(str(row["id"]), confirm, cancel, self.confirm_text(binding), live=True)
        if error is not None:
            raise error
        assert result is not None
        return result

    def confirm(self, token: str, *, tg_user_id: int, chat_id: int, message_id: int) -> dict[str, Any]:
        actor = f"tg:{tg_user_id}"
        error: ApprovalError | None = None
        binding: dict[str, Any] = {}
        self._refuse_if_blocked("confirm_refused")
        with db.transaction(self.conn):
            row = self._token_row(token, (self.confirm_purpose,), chat_id, message_id)
            try:
                self._check_live(row, "armed")
                binding = self._recheck(row, actor)
            except ApprovalError as exc:
                error = exc
            else:
                self.conn.execute(
                    f"UPDATE {self.table} SET status = 'confirmed', confirmed_at = ?, decided_by_tg_user = ?"  # noqa: S608
                    " WHERE id = ?",
                    (to_iso(self._now()), tg_user_id, row["id"]),
                )
                self.audit.append(actor, "approval_confirmed", {"approval_id": row["id"]}, self.cid_of(row))
        if error is not None:
            raise error
        return binding

    def cancel(self, token: str, *, tg_user_id: int, chat_id: int, message_id: int) -> None:
        with db.transaction(self.conn):
            row = self._token_row(token, (self.cancel_purpose,), chat_id, message_id)
            if row["status"] == "armed":
                self.conn.execute(
                    f"UPDATE {self.table} SET status = 'pending', armed_at = NULL WHERE id = ?",  # noqa: S608
                    (row["id"],),
                )
                self.conn.execute(
                    "UPDATE ui_tokens SET used_at = ? WHERE approval_id = ? AND purpose = ? AND used_at IS NULL",
                    (to_iso(self._now()), row["id"], self.confirm_purpose),
                )
                self.audit.append(f"tg:{tg_user_id}", "approval_disarmed", {"approval_id": row["id"]},
                                  self.cid_of(row))  # fmt: skip

    def consume(self, approval_id: str) -> dict[str, Any]:
        """Immediately before the effect: confirmed, unexpired, allowed now, binding unchanged."""
        error: ApprovalError | None = None
        binding: dict[str, Any] = {}
        with db.transaction(self.conn):
            row = self._row(approval_id)
            try:
                self._check_live(row, "confirmed")
                self._refuse_if_blocked("blocked")
                binding = self._recheck(row, "gatekeeper_internal")
            except ApprovalError as exc:
                error = exc
            else:
                self.conn.execute(
                    f"UPDATE {self.table} SET status = 'consumed', consumed_at = ? WHERE id = ?",  # noqa: S608
                    (to_iso(self._now()), approval_id),
                )
                self.tokens._kill_tokens([approval_id])
                self.audit.append("gatekeeper_internal", "approval_consumed", {"approval_id": approval_id},
                                  self.cid_of(row))  # fmt: skip
        if error is not None:
            raise error
        return binding

    def _reject(self, approval_id: str, actor: str, extra: Mapping[str, Any]) -> sqlite3.Row:
        with db.transaction(self.conn):
            row = self._row(approval_id)
            if row["status"] not in OPEN_STATUSES:
                raise ApprovalError("wrong_status", row["status"])
            sets = ", ".join(f"{k} = ?" for k in extra)
            self.conn.execute(
                f"UPDATE {self.table} SET status = 'rejected'{', ' + sets if sets else ''} WHERE id = ?",  # noqa: S608
                (*extra.values(), approval_id),
            )
            self.tokens._kill_tokens([approval_id])
            self.audit.append(actor, "approval_rejected", {"approval_id": approval_id, **extra}, self.cid_of(row))
            return row
