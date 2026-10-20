"""Gate 1 content and effects (CR-004; DESIGN §9.2, §9.5a, §9.5b, §14.11.15).

* ``StoredProposals`` is the ``ProposalSource`` of ``ProposalApprovals``: the bound fields
  ``{proposal_sha256, est_class, user_directed, policy_hash}`` recomputed from the stored version.
* ``ProposalService`` stores scout output (``POST /v1/contributions/{id}/proposal``), runs the
  decision effects (Start work, Adjust / Make it smaller / Split, Skip / Later / Never, Undo) and
  the expiry sweep. Start work is the only way into ``PROPOSAL_APPROVED``; nothing is fetched,
  planned or implemented before it (AC 37).

Estimates are model output: the gatekeeper classifies the numbers itself (``size.classify``) and
keeps the larger of that and scout's class. Autonomous proposals above the effective
``autonomous_max_class`` are refused; no proposal above the hard ceiling is accepted.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

from ossbot.common.clock import from_iso, to_iso
from ossbot.common.hashing import canonical_json, sha256_hex
from ossbot.common.schemas import ProposalSubmit, SizeClassName, State
from ossbot.common.size import SIZE_CLASSES, classify
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalError
from ossbot.gatekeeper.state_machine import Actor

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

S = State
RevisionKind = Literal["adjust", "smaller", "split"]
SMALLER_TEXT = "Reduce the scope to fit class {cls}; keep the fix minimal"
SPLIT_TEXT = "Split this into parts that each stay under the ceiling; propose part 1 first"
SNOOZE_DAYS_MAX = 90


class ProposalRefused(RuntimeError):
    def __init__(self, status: int, code: str, detail: str = "") -> None:
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(f"{status} {code}{': ' + detail if detail else ''}")


def class_index(cls: str) -> int:
    return SIZE_CLASSES.index(cls)


def larger(a: str, b_: str) -> SizeClassName:
    return a if class_index(a) >= class_index(b_) else b_  # type: ignore[return-value]


class StoredProposals:
    """ProposalSource over the ``proposals`` table (fails closed on a missing version)."""

    def __init__(self, policy_hash: Any) -> None:
        self.policy_hash = policy_hash  # Callable[[], str]: the current config snapshot hash

    def fields(self, conn: sqlite3.Connection, cid: str, version: int) -> dict[str, Any]:
        row = db.one(
            conn,
            "SELECT p.proposal_sha256, p.est_class, c.source FROM proposals p JOIN contributions c ON c.id = p.cid"
            " WHERE p.cid = ? AND p.version = ?",
            (cid, version),
        )
        if row is None:
            raise ApprovalError("proposal_unavailable", f"{cid} v{version}")
        return {
            "proposal_sha256": row["proposal_sha256"],
            "est_class": row["est_class"],
            "user_directed": row["source"] == "user",
            "policy_hash": self.policy_hash(),
        }


class ProposalService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.conn = ctx.conn

    @property
    def svc(self) -> Any:
        return self.ctx.decisions.proposals

    def _contribution(self, cid: str) -> sqlite3.Row:
        row = db.one(self.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
        if row is None:
            raise ProposalRefused(404, "not_found", cid)
        return row

    def pacing(self) -> dict[str, Any]:
        """``proposal.max_per_day`` over the last 24 h (first versions of autonomous items, §9.5b).

        ``next_slot_at`` is when the oldest counted proposal leaves the window (None while slots
        remain); the runner reads it to hold autonomous scouting instead of scouting into a 429.
        """
        now = self.ctx.clock.now()
        cap = self.ctx.config.policy.proposal.max_per_day
        created = [
            str(r[0])
            for r in self.conn.execute(
                "SELECT p.created_at FROM proposals p JOIN contributions c ON c.id = p.cid"
                " WHERE p.version = 1 AND c.source != 'user' AND p.created_at > ? ORDER BY p.created_at",
                (to_iso(now - timedelta(days=1)),),
            )
        ]
        remaining = max(0, cap - len(created))
        next_slot = None
        if remaining == 0 and 0 < cap <= len(created):
            next_slot = to_iso(from_iso(created[len(created) - cap]) + timedelta(days=1))
        return {"max_per_day": cap, "used": len(created), "remaining": remaining, "next_slot_at": next_slot}

    # ------------------------------------------------------------------- store
    def submit(self, cid: str, req: ProposalSubmit, actor: str) -> dict[str, Any]:
        ctx = self.ctx
        policy = ctx.config.policy
        prop = req.proposal
        est = prop.estimate
        est_class = larger(classify(est.code_lines, est.files), est.size_class)
        ceiling = policy.diff
        if (
            est.files > ceiling.max_files
            or est.code_lines > ceiling.max_changed_lines
            or (est.test_lines > ceiling.max_test_lines)
        ):
            raise ProposalRefused(422, "above_ceiling", f"{est.files} files, {est.code_lines}/{est.test_lines} lines")
        now = ctx.clock.now()
        with db.transaction(self.conn):
            row = self._contribution(cid)
            state = State(row["state"])
            if state not in {S.SCOUTING, S.PROPOSED, S.RESCOPE_REQUIRED}:
                raise ProposalRefused(409, "not_scouting", state.value)
            current = row["proposal_version"]
            if state is not S.SCOUTING and req.revises != current:
                raise ProposalRefused(409, "superseded", f"revises {req.revises}, current v{current}")
            user = row["source"] == "user"
            if not user:  # autonomous only (Amendment B): size preference, filters, never, pacing
                prefs = ctx.discovery_prefs.effective(row["upstream"])
                if prefs.never_repo:
                    raise ProposalRefused(422, "never_repo", row["upstream"])
                if class_index(est_class) > class_index(prefs.autonomous_max_class):
                    raise ProposalRefused(422, "above_autonomous_max", f"{est_class} > {prefs.autonomous_max_class}")
                kinds = prefs.filters
                if prop.kind in kinds.excluded_kinds or (kinds.allowed_kinds and prop.kind not in kinds.allowed_kinds):
                    raise ProposalRefused(422, "kind_filtered", prop.kind)
                if current is None:
                    pacing = self.pacing()
                    if pacing["remaining"] == 0:
                        raise ProposalRefused(429, "proposal_daily_cap", str(pacing["next_slot_at"]))
            version = int(current or 0) + 1
            doc = prop.model_dump(by_alias=True)
            canonical = canonical_json(doc).decode("utf-8")
            digest = sha256_hex(canonical.encode("utf-8"))
            self.conn.execute(
                "INSERT INTO proposals (cid, version, proposal_json, proposal_sha256, est_class, feedback,"
                " created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, version, canonical, digest, est_class, req.feedback, actor, to_iso(now)),
            )
            self.conn.execute(
                "UPDATE contributions SET proposal_version = ?, est_class = ?, kind = ?, proposal_feedback_json = NULL,"
                " updated_at = ? WHERE id = ?",
                (version, est_class, prop.kind, to_iso(now), cid),
            )
            self.svc._invalidate("cid = ? AND status IN ('pending','armed','confirmed')", (cid,),
                                 "new_proposal_version", actor)  # fmt: skip
            if state is not S.PROPOSED:
                ctx.state_machine.transition(cid, state, S.PROPOSED, Actor.GATEKEEPER, {"version": version}, actor)
            approval_id = self.svc.create(cid, version, tg_chat_id=ctx.telegram_chat_id, actor=actor)
            ctx.audit.append(actor, "proposal_stored", {"version": version, "proposal_sha256": digest,
                                                         "est_class": est_class}, cid)  # fmt: skip
        ctx.on_approval_created(approval_id)
        return {"version": version, "approval_id": approval_id, "est_class": est_class, "proposal_sha256": digest}

    def get(self, cid: str, version: int | None = None) -> dict[str, Any]:
        row = self._contribution(cid)
        version = version or row["proposal_version"]
        found = db.one(self.conn, "SELECT * FROM proposals WHERE cid = ? AND version = ?", (cid, version))
        if found is None:
            raise ProposalRefused(404, "no_proposal")
        out = dict(found)
        out["proposal"] = json.loads(out.pop("proposal_json"))
        out["feedback_pending"] = json.loads(row["proposal_feedback_json"]) if row["proposal_feedback_json"] else None
        return out

    def own_open_pr(self, cid: str) -> int | None:
        """Our newest open PR on the contribution's repo from another contribution (one-open-PR rule)."""
        row = db.one(self.conn, "SELECT o.pr_number FROM contributions c JOIN contributions o ON o.upstream ="
                     " c.upstream AND o.id != c.id WHERE c.id = ? AND o.pr_number IS NOT NULL AND o.state IN"
                     " ('SUBMITTED','WATCHING') ORDER BY o.updated_at DESC LIMIT 1", (cid,))  # fmt: skip
        return int(row["pr_number"]) if row is not None else None

    # ----------------------------------------------------------------- effects
    def start(self, approval_id: str, actor: str) -> dict[str, Any]:
        """✅ Start work, after Confirm: consume (binding recomputed, refusals) and enter PROPOSAL_APPROVED."""
        ctx = self.ctx
        with db.transaction(self.conn):
            binding = self.svc.consume(approval_id)
            cid = str(binding["cid"])
            row = self._contribution(cid)
            if row["proposal_version"] != binding["version"]:
                raise ProposalRefused(409, "superseded")
            evidence = {"approval_id": approval_id, "version": binding["version"]}
            ctx.state_machine.transition(cid, S.PROPOSED, S.PROPOSAL_APPROVED, Actor.HUMAN, evidence, actor)
            self.conn.execute("UPDATE contributions SET approved_class = ? WHERE id = ?", (binding["est_class"], cid))
            open_pr = self.own_open_pr(cid)
            if row["source"] == "user" and open_pr is not None:
                # the Confirm panel said "confirming overrides the limit for this item" (§14.11.19)
                ctx.audit.append(actor, "open_pr_override", {"approval_id": approval_id, "open_pr": open_pr}, cid)
        return {"cid": cid, "state": S.PROPOSAL_APPROVED.value, "approved_class": binding["est_class"]}

    def request_revision(self, cid: str, version: int, kind: RevisionKind, text: str, actor: str) -> dict[str, Any]:
        """✏️ Adjust plan / 📏 Make it smaller / ✂️ Split: feedback for the runner's next scout run."""
        with db.transaction(self.conn):
            row = self._contribution(cid)
            state = State(row["state"])
            allowed = {S.PROPOSED} if kind == "adjust" else {S.PROPOSED, S.RESCOPE_REQUIRED}
            if state not in allowed:
                raise ProposalRefused(409, "not_open", state.value)
            if row["proposal_version"] != version:
                raise ProposalRefused(409, "superseded")
            if kind == "adjust":
                text = text.strip()
                if not text or len(text) > 2000:
                    raise ProposalRefused(400, "bad_feedback", "1-2000 characters")
            elif kind == "smaller" and state is S.RESCOPE_REQUIRED:
                # §14.11.17: back to the class the user approved (xs included), not one below it
                if row["approved_class"] is None:
                    raise ProposalRefused(409, "no_approved_class")
                text = SMALLER_TEXT.format(cls=row["approved_class"])
            elif kind == "smaller":
                current = row["est_class"]
                if current is None or class_index(current) == 0:
                    raise ProposalRefused(409, "already_smallest")
                text = SMALLER_TEXT.format(cls=SIZE_CLASSES[class_index(current) - 1])
            else:
                text = SPLIT_TEXT
            feedback = {"kind": kind, "text": text, "version": version, "at": to_iso(self.ctx.clock.now())}
            self.conn.execute(
                "UPDATE contributions SET proposal_feedback_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(feedback), feedback["at"], cid),
            )
            self.ctx.audit.append(actor, "proposal_revision_requested", {"kind": kind, "version": version}, cid)
        return feedback

    def decline(self, approval_id: str, reason: str, actor: str, *, snooze_days: int | None = None) -> dict[str, Any]:
        """⏭ Skip, ⏰ Later, 🚫 Never (after its confirm): one transaction (§14.11.15 Declining)."""
        ctx = self.ctx
        if reason == "snoozed" and not (snooze_days and 1 <= snooze_days <= SNOOZE_DAYS_MAX):
            raise ProposalRefused(400, "bad_snooze", f"1-{SNOOZE_DAYS_MAX} days")
        now = ctx.clock.now()
        with db.transaction(self.conn):
            approval = self.svc._row(approval_id)
            if approval["status"] == "armed":
                raise ProposalRefused(409, "cancel_confirm_first")
            if approval["status"] != "pending":
                raise ProposalRefused(409, "wrong_status", str(approval["status"]))
            cid = str(approval["cid"])
            row = self._contribution(cid)
            snooze_end = now + timedelta(days=snooze_days) if reason == "snoozed" and snooze_days else None
            self.svc.reject(approval_id, reason=reason, actor=actor, snooze_until=snooze_end)
            to = S.PROPOSAL_SNOOZED if reason == "snoozed" else S.PROPOSAL_REJECTED
            ctx.state_machine.transition(cid, S.PROPOSED, to, Actor.HUMAN, {"reason": reason}, actor)
            until = to_iso(snooze_end) if snooze_end is not None else None
            self.conn.execute(
                "UPDATE contributions SET declined_at = ?, declined_reason = ?, snooze_until = ? WHERE id = ?",
                (to_iso(now), reason, until, cid),
            )
            ctx.audit.append(actor, "proposal_declined", {"approval_id": approval_id, "reason": reason}, cid)
            if reason == "never_repo":
                ctx.discovery_prefs.never_repo(str(row["upstream"]), actor)
            elif reason == "never_kind" and row["kind"]:
                ctx.discovery_prefs.never_kind(str(row["kind"]), actor)
        return {"cid": cid, "state": to.value, "snooze_until": until}

    def undo(self, cid: str, approval_id: str, actor: str) -> list[str]:
        """↩️ Undo (Skip and Later only, 10 min): back to PROPOSED, then a fresh card (expiry kept)."""
        with db.transaction(self.conn):
            row = self._contribution(cid)
            state = State(row["state"])
            if state not in {S.PROPOSAL_REJECTED, S.PROPOSAL_SNOOZED}:
                raise ProposalRefused(409, "too_late", state.value)
            declined = self.svc._row(approval_id)
            if declined["cid"] != cid or declined["status"] != "rejected":
                raise ProposalRefused(409, "not_declined")
            self.ctx.state_machine.transition(cid, state, S.PROPOSED, Actor.HUMAN, {"undo": approval_id}, actor)
        return self.ctx.reissue_proposal(cid, actor, keep_expiry=True)

    def continue_rescope(self, cid: str, actor: str, *, at_measured: bool,
                         expect: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
        """RESCOPE_REQUIRED -> PACKAGING (✅ Continue at <class> or 🔁 Re-check, FEAT-009 buttons);
        the runner re-sends /package for the same rev and the size check runs again.

        Continue (``at_measured``) is a proposal Start work (§14.11.17): refused while paused, killed,
        draining or down (§14.11.15); Re-check only re-runs the size check. ``expect`` (the binding the
        Confirm panel showed, ``rescope_binding``) must still match the stored report. The edge is G
        (the gatekeeper moves the item); the human who decided is recorded in the evidence."""
        refusal = self.ctx.decisions.proposals.refusal() if at_measured else None
        if refusal is not None:
            raise ProposalRefused(409, "refused", refusal)
        with db.transaction(self.conn):
            row = self._contribution(cid)
            if State(row["state"]) is not S.RESCOPE_REQUIRED:
                raise ProposalRefused(409, "not_open", str(row["state"]))
            report = json.loads(row["size_report_json"] or "{}")
            if expect is not None and rescope_binding(row["size_report_json"]) != expect:
                raise ProposalRefused(409, "superseded")
            if at_measured and report.get("measured_class"):
                self.conn.execute("UPDATE contributions SET approved_class = ? WHERE id = ?",
                                  (report["measured_class"], cid))  # fmt: skip
            evidence = {"continue": at_measured, "decided_by": "human",
                        "measured_class": report.get("measured_class")}  # fmt: skip
            self.ctx.state_machine.transition(cid, S.RESCOPE_REQUIRED, S.PACKAGING, Actor.GATEKEEPER, evidence,
                                              actor)  # fmt: skip
        return {"cid": cid, "state": S.PACKAGING.value}

    # ------------------------------------------------------------------- sweep
    def sweep_expired(self) -> list[str]:
        """PROPOSED with no open approval left (ttl elapsed) -> PROPOSAL_EXPIRED (re-scouted later)."""
        moved: list[str] = []
        now = to_iso(self.ctx.clock.now())
        for row in self.conn.execute("SELECT id FROM contributions WHERE state = 'PROPOSED'").fetchall():
            live = self.conn.execute(
                "SELECT 1 FROM proposal_approvals WHERE cid = ? AND status IN ('pending','armed','confirmed')"
                " AND expires_at > ? LIMIT 1",
                (row["id"], now),
            ).fetchone()
            expired = self.conn.execute(
                "SELECT 1 FROM proposal_approvals WHERE cid = ? AND status = 'expired' LIMIT 1", (row["id"],)
            ).fetchone()
            if live is not None or expired is None:
                continue
            self.ctx.state_machine.transition(
                str(row["id"]), S.PROPOSED, S.PROPOSAL_EXPIRED, Actor.GATEKEEPER, {"reason": "proposal_ttl"}
            )
            moved.append(str(row["id"]))
        return moved


def rescope_binding(size_report_json: str | None) -> dict[str, Any]:
    """What a rescope Confirm panel binds (§14.11.17): the rev, the measured class and the report hash."""
    raw = size_report_json or "{}"
    try:
        report = json.loads(raw)
    except ValueError:
        report = {}
    if not isinstance(report, dict):
        report = {}
    rev = report.get("rev")
    return {"report_rev": rev if isinstance(rev, int) else None, "measured_class": report.get("measured_class"),
            "report_sha256": sha256_hex(raw.encode())}  # fmt: skip


def snooze_due(row: sqlite3.Row, now: Any) -> bool:
    until = row["snooze_until"]
    return until is None or from_iso(until) <= now
