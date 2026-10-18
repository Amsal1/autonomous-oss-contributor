"""Gate 1: the proposal decision service (CR-004, DESIGN §9.5b, §14.11.15, §14.18.4.2).

``p_`` ids in ``proposal_approvals``. ✅ Start work is two taps (``proposal_start`` ->
``proposal_confirm``/``proposal_cancel``) on single-use, message-bound tokens and binds
``{proposal_sha256, version, est_class}``; the binding is recomputed at Approve, at Confirm and in
``consume`` right before the effect (enqueueing the plan stage, PLAN item 29). Start work is refused
while paused, killed, draining or down. A proposal card has no parts: it is armable once sent.

The proposal content (scout output, versions) is ``gatekeeper.proposals.StoredProposals`` (PLAN item
29); without a source the service is fail-closed (no binding can be computed, so nothing can be
approved). The effects (Start work, declines, Undo) are ``gatekeeper.proposals.ProposalService``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from ossbot.common.clock import from_iso
from ossbot.common.ids import new_proposal_approval_id
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalError
from ossbot.gatekeeper.decisions import TableDecisions

RejectReason = Literal["skipped", "snoozed", "never_repo", "never_kind"]
# §14.11.15 binding: {v, kind, cid, version} + these (proposals.StoredProposals, PLAN item 29)
BOUND_FIELDS: tuple[str, ...] = ("proposal_sha256", "est_class", "user_directed", "policy_hash")
REJECT_REASONS: tuple[str, ...] = ("skipped", "snoozed", "never_repo", "never_kind")


class ProposalSource(Protocol):
    """Recomputes the bound proposal fields from the stored proposal ``(cid, version)``."""

    def fields(self, conn: sqlite3.Connection, cid: str, version: int) -> dict[str, Any]: ...


class NoProposals:
    """Fail closed until PLAN item 29 stores proposals."""

    def fields(self, conn: sqlite3.Connection, cid: str, version: int) -> dict[str, Any]:
        raise ApprovalError("proposal_unavailable", f"{cid} v{version}")


class ProposalApprovals(TableDecisions):
    table = "proposal_approvals"
    id_prefix = "p"
    kind = "proposal"
    approve_purpose = "proposal_start"
    confirm_purpose = "proposal_confirm"
    cancel_purpose = "proposal_cancel"
    card_title = "💡 Proposal"

    def __init__(self, *args: Any, source: ProposalSource | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.source: ProposalSource = source or NoProposals()

    def subject(self, row: sqlite3.Row) -> dict[str, Any]:
        return {"cid": row["cid"], "version": int(row["version"])}

    def fields(self, row: sqlite3.Row) -> dict[str, Any]:
        fields = self.source.fields(self.conn, str(row["cid"]), int(row["version"]))
        want = set(BOUND_FIELDS)
        if set(fields) != want or any(v is None or v == "" for v in fields.values()):
            raise ApprovalError("binding_fields", f"proposal fields {sorted(fields)} != {sorted(want)}")
        return fields

    def cid_of(self, row: sqlite3.Row) -> str | None:
        return str(row["cid"])

    def confirm_text(self, binding: Mapping[str, Any]) -> str:
        return (
            f"Confirm: start work on {binding['cid']} (proposal v{binding['version']}, class {binding['est_class']})."
            " Nothing is published; the PR still needs its own approval. Expires in 5 min."
        )

    def card_rows(self, approval_id: str, tokens: dict[str, str]) -> list[list[tuple[str, str]]]:
        return [[("✅ Start work", tokens[self.approve_purpose])]]

    # -------------------------------------------------------------------- lifecycle
    def create(self, cid: str, version: int, *, tg_chat_id: int, actor: str = "system",
               expires_at: datetime | None = None) -> str:  # fmt: skip
        approval_id = new_proposal_approval_id()
        expiry = self._expiry(expires_at)
        with db.transaction(self.conn):
            if db.one(self.conn, "SELECT 1 FROM contributions WHERE id = ?", (cid,)) is None:
                raise ApprovalError("unknown_contribution", cid)
            binding = {"v": 1, "kind": self.kind, "cid": cid, "version": version,
                       **self.source.fields(self.conn, cid, version)}  # fmt: skip
            self._insert(approval_id, {"cid": cid, "version": version}, binding, tg_chat_id, expiry, actor, cid)
        return approval_id

    def reject(self, approval_id: str, reason: RejectReason, actor: str) -> None:
        """Skip, Later and Never (after its confirm); the kit buttons only collect the choice."""
        if reason not in REJECT_REASONS:
            raise ApprovalError("bad_reason", str(reason))
        self._reject(approval_id, actor, {"rejected_reason": reason})

    def reissue(self, cid: str, *, actor: str, keep_expiry: bool = False) -> list[str]:
        """Re-send the open proposal card of ``cid`` (§14.18.4.3; only ``AppContext.reissue_proposal``
        calls this and publishes after the commit). Eligible: the contribution in ``PROPOSED`` with
        no ``confirmed`` proposal approval."""
        now = self._now()
        floor = now + timedelta(seconds=self.confirm_ttl_seconds())
        with db.transaction(self.conn):
            contrib = db.one(self.conn, "SELECT state FROM contributions WHERE id = ?", (cid,))
            if contrib is None:
                raise ApprovalError("unknown_contribution", cid)
            if db.one(self.conn, "SELECT 1 FROM proposal_approvals WHERE cid = ? AND status = 'confirmed'", (cid,)):
                raise ApprovalError("submitting", cid)
            newest = db.one(
                self.conn, "SELECT * FROM proposal_approvals WHERE cid = ? ORDER BY version DESC, expires_at DESC"
                " LIMIT 1", (cid,),
            )  # fmt: skip
            if contrib["state"] != "PROPOSED" or newest is None:
                raise ApprovalError("nothing_to_resend", str(contrib["state"]))
            previous = from_iso(newest["expires_at"])
            inherit = previous if keep_expiry and previous > floor else None
            self._invalidate("cid = ? AND status IN ('pending','armed')", (cid,), "reissued", actor)
            version = int(newest["version"])
            return [self.create(cid, version, tg_chat_id=self.chat_id(), actor=actor, expires_at=inherit)]

    def reissue_invalidated(self, cids: list[str], *, actor: str) -> list[str]:
        """After a reload: fresh cards (expiry kept) for proposals that are still open (§14.18.4.2)."""
        out: list[str] = []
        for cid in dict.fromkeys(cids):
            try:
                out += self.reissue(cid, actor=actor, keep_expiry=True)
            except ApprovalError:
                continue
        return out
