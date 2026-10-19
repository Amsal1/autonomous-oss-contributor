"""The skill activation decision service (CR-002, DESIGN §14.15, §14.18.4.2).

``s_`` ids in ``skill_approvals``. Activating a skill Hermes staged is two taps (``skill_approve``
-> ``skill_confirm``/``skill_cancel``; ``skill_reject`` rejects) on single-use, message-bound
tokens; ``skill_diff`` and ``skill_ask`` are multi-use view purposes. The binding (the staged
content hash from the root hasher, PLAN item 36) is recomputed at Approve, at Confirm and in
``consume`` right before the effect (the root copy). Refused while paused or killed. Skill cards
have parts (files, diff), so Approve is minted only after every part was delivered; parts are
counted by ``Outbox.mark_sent`` (``s_`` prefix). A tier switch invalidates every open skill
approval with ``tier_changed`` (``ControlService.set_hermes_tier``).

The staged content (``skill_reviews``) arrives with PLAN item 36 (FEAT-007); until then the
``SkillSource`` is fail-closed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Protocol

from ossbot.common.clock import from_iso
from ossbot.common.ids import new_skill_approval_id
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalError
from ossbot.gatekeeper.decisions import TableDecisions
from ossbot.gatekeeper.telegram import keyboards


class SkillSource(Protocol):
    def fields(self, conn: sqlite3.Connection, skill_review_id: str) -> dict[str, Any]:
        """The bound fields recomputed from the staged skill (e.g. ``content_sha256``, ``tier``)."""
        ...

    def reissuable(self, conn: sqlite3.Connection, skill: str) -> str | None:
        """The ``skill_review_id`` while ``skill`` is still staged and within the tier, else None."""
        ...


class NoSkills:
    """Fail closed until PLAN item 36 stages skills."""

    def fields(self, conn: sqlite3.Connection, skill_review_id: str) -> dict[str, Any]:
        raise ApprovalError("skill_unavailable", skill_review_id)

    def reissuable(self, conn: sqlite3.Connection, skill: str) -> str | None:
        return None


class SkillApprovals(TableDecisions):
    table = "skill_approvals"
    id_prefix = "s"
    kind = "skill"
    approve_purpose = "skill_approve"
    confirm_purpose = "skill_confirm"
    cancel_purpose = "skill_cancel"
    reject_purposes = ("skill_reject",)
    view_purposes = ("skill_diff", "skill_ask")
    has_parts = True
    card_title = "🧩 Skill"

    def __init__(self, *args: Any, source: SkillSource | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.source: SkillSource = source or NoSkills()

    def subject(self, row: sqlite3.Row) -> dict[str, Any]:
        return {"skill": row["skill"], "version": int(row["version"]), "skill_review_id": row["skill_review_id"]}

    def fields(self, row: sqlite3.Row) -> dict[str, Any]:
        fields = self.source.fields(self.conn, str(row["skill_review_id"]))
        if not fields or any(v in (None, "") for v in fields.values()):
            raise ApprovalError("binding_fields", "skill fields empty")
        return fields

    def confirm_text(self, binding: Mapping[str, Any]) -> str:
        return f"Confirm: activate skill {binding['skill']} v{binding['version']} for Hermes. Expires in 5 min."

    def card_rows(self, approval_id: str, tokens: dict[str, str]) -> keyboards.Rows:
        return [[("✅ Activate", tokens[self.approve_purpose]), ("❌ Reject", tokens["skill_reject"])]]

    # -------------------------------------------------------------------- lifecycle
    def create(self, skill_review_id: str, skill: str, version: int, *, tg_chat_id: int, body_parts_total: int,
               actor: str = "system", expires_at: datetime | None = None) -> str:  # fmt: skip
        if body_parts_total < 1:
            raise ApprovalError("bad_parts")
        approval_id = new_skill_approval_id()
        expiry = self._expiry(expires_at)
        with db.transaction(self.conn):
            subject = {"skill": skill, "version": version, "skill_review_id": skill_review_id}
            binding = {"v": 1, "kind": self.kind, **subject, **self.source.fields(self.conn, skill_review_id)}
            self._insert(approval_id, {**subject, "body_parts_total": body_parts_total}, binding, tg_chat_id, expiry,
                         actor, None)  # fmt: skip
        return approval_id

    def reject(self, token: str, *, tg_user_id: int, chat_id: int, message_id: int) -> None:
        with db.transaction(self.conn):
            row = self._token_row(token, self.reject_purposes, chat_id, message_id)
            self._reject(str(row["id"]), f"tg:{tg_user_id}", {"decided_by_tg_user": tg_user_id})

    def reissue(self, skill: str, *, actor: str, keep_expiry: bool = False) -> list[str]:
        """Re-send the skill card (only ``AppContext.reissue_skill`` calls this and publishes after the
        commit). Eligible: still staged and within the tier, no ``confirmed`` skill approval. The new
        approval carries the skill card's own part count."""
        now = self._now()
        floor = now + timedelta(seconds=self.confirm_ttl_seconds())
        with db.transaction(self.conn):
            if db.one(self.conn, "SELECT 1 FROM skill_approvals WHERE skill = ? AND status = 'confirmed'", (skill,)):
                raise ApprovalError("submitting", skill)
            review_id = self.source.reissuable(self.conn, skill)
            newest = db.one(self.conn, "SELECT * FROM skill_approvals WHERE skill = ? ORDER BY expires_at DESC LIMIT 1",
                            (skill,))  # fmt: skip
            if review_id is None or newest is None:
                raise ApprovalError("nothing_to_resend", skill)
            previous = from_iso(newest["expires_at"])
            inherit = previous if keep_expiry and previous > floor else None
            self._invalidate("skill = ? AND status IN ('pending','armed')", (skill,), "reissued", actor)
            return [
                self.create(review_id, skill, int(newest["version"]), tg_chat_id=self.chat_id(), actor=actor,
                            body_parts_total=int(newest["body_parts_total"]), expires_at=inherit)
            ]  # fmt: skip

    def reissue_invalidated(self, skills: list[str], *, actor: str) -> list[str]:
        """After a reload: fresh cards (expiry kept) while the skill is still staged and in the tier."""
        out: list[str] = []
        for skill in dict.fromkeys(skills):
            try:
                out += self.reissue(skill, actor=actor, keep_expiry=True)
            except ApprovalError:
                continue
        return out
