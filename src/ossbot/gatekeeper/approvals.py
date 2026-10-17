"""Approval binding, lifecycle and invalidation (DESIGN §12.4, I1, I3, I7).

* Binding v2 = common ``{v, kind, cid, upstream, dry_run, policy_hash}`` + exactly the per-kind
  fields. ``binding_hash = sha256(canonical_json(binding))``.
* ``dry_run`` and ``policy_hash`` are always recomputed from the current control state; the
  binding is recomputed at Approve, at Confirm and immediately before the first write.
* Texts are normalized once at storage time (CRLF/CR -> LF, NFC; titles also lose trailing
  whitespace). Hash functions take bytes and never normalize (N3).
* Lifecycle: pending -> armed -> confirmed -> consumed; or invalidated / expired / rejected.
* Every invalidation runs inside the caller's transaction and kills the approval's UI tokens.
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from ossbot.common.clock import Clock, from_iso, to_iso
from ossbot.common.config import Limits, ProfileConfig
from ossbot.common.control_flags import read_flags
from ossbot.common.hashing import canonical_json, sha256_hex, sha256_text
from ossbot.common.ids import TOKEN_RE, new_approval_id, new_token
from ossbot.common.schemas import OutboundKind
from ossbot.gatekeeper import db
from ossbot.gatekeeper.audit import AuditLog

BINDING_VERSION = 2
COMMON_FIELDS = ("v", "kind", "cid", "upstream", "dry_run", "policy_hash")
KIND_FIELDS: dict[str, tuple[str, ...]] = {
    "open_pr": (
        "base_ref", "base_sha", "upstream_base_tip", "head_sha", "branch", "bundle_sha256",
        "patch_sha256", "commit_message_sha256", "title_sha256", "body_sha256", "fork_owner",
    ),
    "push_update": (
        "pr_number", "branch", "fork_owner", "old_head_sha", "head_sha", "bundle_sha256",
        "patch_sha256", "commit_message_sha256",
    ),
    "comment": ("target_number", "body_sha256"),
    "issue_claim": ("issue_number", "body_sha256"),
}  # fmt: skip
OPEN_STATUSES = ("pending", "armed", "confirmed")
CARD_PURPOSES = ("approve", "reject", "reject_reason", "changes", "edit_title", "edit_body", "clear_edits")


class ApprovalError(RuntimeError):
    """A decision could not be applied. ``code`` is stable and shown/logged."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


class Superseded(ApprovalError):
    """Content or mode changed since the card was shown: the approval was invalidated."""


# --------------------------------------------------------------------------- normalization
def normalize_text(text: str, *, title: bool = False) -> str:
    out = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    return out.rstrip() if title else out


# ------------------------------------------------------------------------------- binding
def build_binding(
    kind: OutboundKind, *, cid: str, upstream: str, dry_run: bool, policy_hash: str, fields: Mapping[str, Any]
) -> dict[str, Any]:
    want = set(KIND_FIELDS[kind])
    have = set(fields)
    if want != have:
        raise ApprovalError("binding_fields", f"missing {sorted(want - have)} extra {sorted(have - want)}")
    for key, value in fields.items():
        if value is None or value == "":
            raise ApprovalError("binding_fields", f"{key} is empty")
    binding: dict[str, Any] = {
        "v": BINDING_VERSION,
        "kind": kind,
        "cid": cid,
        "upstream": upstream,
        "dry_run": bool(dry_run),
        "policy_hash": policy_hash,
    }
    binding.update(fields)
    return binding


def binding_hash(binding: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json(dict(binding)))


@dataclass(frozen=True)
class ArtifactHashes:
    bundle_sha256: str
    patch_sha256: str
    commit_message_sha256: str


class ArtifactSource(Protocol):
    """Re-derives the bound artifact hashes from the gatekeeper's own mirror (FEAT-002 gitops)."""

    def hashes(self, cid: str, rev: int, kind: OutboundKind) -> ArtifactHashes: ...


class NoArtifacts:
    """Fail-closed default until the git TCB lands: any PR-kind binding cannot be recomputed."""

    def hashes(self, cid: str, rev: int, kind: OutboundKind) -> ArtifactHashes:
        raise ApprovalError("artifacts_unavailable", f"{cid} r{rev}")


def compute_binding(conn: sqlite3.Connection, action_id: str, artifacts: ArtifactSource) -> dict[str, Any]:
    """Recompute the binding from the DB rows, the current control state and the artifacts."""
    action = db.one(conn, "SELECT * FROM outbound_actions WHERE id = ?", (action_id,))
    if action is None:
        raise ApprovalError("unknown_action", action_id)
    contrib = db.one(conn, "SELECT * FROM contributions WHERE id = ?", (action["cid"],))
    if contrib is None:
        raise ApprovalError("unknown_contribution", str(action["cid"]))
    payload: dict[str, Any] = json.loads(action["payload_json"])
    kind: OutboundKind = action["kind"]
    fields: dict[str, Any]
    if kind == "comment":
        fields = {"target_number": payload["target_number"], "body_sha256": sha256_text(payload["body"])}
    elif kind == "issue_claim":
        fields = {"issue_number": payload["target_number"], "body_sha256": sha256_text(payload["body"])}
    else:
        hashes = artifacts.hashes(contrib["id"], int(payload["rev"]), kind)
        common = {
            "head_sha": payload["head_sha"],
            "branch": payload["branch"],
            "fork_owner": payload["fork_owner"],
            "bundle_sha256": hashes.bundle_sha256,
            "patch_sha256": hashes.patch_sha256,
            "commit_message_sha256": hashes.commit_message_sha256,
        }
        if kind == "open_pr":
            title = contrib["pr_title_override"] if contrib["pr_title_override"] is not None else contrib["pr_title"]
            body = contrib["pr_body_override"] if contrib["pr_body_override"] is not None else contrib["pr_body"]
            fields = {
                **common,
                "base_ref": contrib["base_ref"],
                "base_sha": contrib["base_sha"],
                "upstream_base_tip": contrib["upstream_base_tip"],
                "title_sha256": sha256_text(title or ""),
                "body_sha256": sha256_text(body or ""),
            }
        else:
            fields = {**common, "pr_number": payload["pr_number"], "old_head_sha": payload["old_head_sha"]}
    return build_binding(
        kind,
        cid=contrib["id"],
        upstream=contrib["upstream"],
        dry_run=db.current_dry_run(conn),
        policy_hash=db.current_policy_hash(conn),
        fields=fields,
    )


# ------------------------------------------------------------------------------- service
@dataclass(frozen=True)
class Invalidated:
    approval_id: str
    action_id: str
    cid: str


@dataclass(frozen=True)
class ArmResult:
    approval_id: str
    confirm_token: str
    cancel_token: str
    confirm_text: str
    live: bool


class ApprovalService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        audit: AuditLog,
        control_dir: Path,
        limits: Callable[[], Limits],
        profile: Callable[[], ProfileConfig],
        artifacts: ArtifactSource | None = None,
    ) -> None:
        self.conn = conn
        self.clock = clock
        self.audit = audit
        self.control_dir = control_dir
        self.limits = limits
        self.profile = profile
        self.artifacts: ArtifactSource = artifacts or NoArtifacts()

    # ---------------------------------------------------------------------- helpers
    def _now(self) -> datetime:
        return self.clock.now()

    def _approval(self, approval_id: str) -> sqlite3.Row:
        row = db.one(self.conn, "SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if row is None:
            raise ApprovalError("unknown_approval", approval_id)
        return row

    def system_running(self) -> bool:
        flags = read_flags(self.control_dir)
        row = db.read_control_row(self.conn)
        return flags.running and row is not None and row["state"] == "running"

    def _kill_tokens(self, approval_ids: list[str]) -> None:
        now = to_iso(self._now())
        for approval_id in approval_ids:
            self.conn.execute(
                "UPDATE ui_tokens SET used_at = ? WHERE approval_id = ? AND used_at IS NULL",
                (now, approval_id),
            )

    def _invalidate(self, where: str, params: tuple[Any, ...], reason: str, actor: str) -> list[Invalidated]:
        with db.transaction(self.conn):
            rows = self.conn.execute(
                "SELECT a.id, a.action_id, o.cid FROM approvals a JOIN outbound_actions o ON o.id = a.action_id"
                f" WHERE {where}",
                params,
            ).fetchall()
            out = [Invalidated(r["id"], r["action_id"], r["cid"]) for r in rows]
            for item in out:
                self.conn.execute(
                    "UPDATE approvals SET status = 'invalidated', invalidated_reason = ? WHERE id = ?",
                    (reason, item.approval_id),
                )
                self.audit.append(
                    actor,
                    "approval_invalidated",
                    {"approval_id": item.approval_id, "reason": reason},
                    item.cid,
                )
            self._kill_tokens([i.approval_id for i in out])
        return out

    # ------------------------------------------------------------------- invalidation
    def invalidate_all_open(self, reason: str, actor: str = "system") -> list[Invalidated]:
        """Dry-run toggle (either direction) or a new config snapshot."""
        return self._invalidate("a.status IN ('pending','armed','confirmed')", (), reason, actor)

    def invalidate_contribution(self, cid: str, reason: str, actor: str = "system") -> list[Invalidated]:
        """New package rev, entering IMPLEMENTING, a title/body edit, a live-PR redirect."""
        return self._invalidate("a.status IN ('pending','armed','confirmed') AND o.cid = ?", (cid,), reason, actor)

    def invalidate_armed_confirmed(self, reason: str, actor: str = "system") -> list[Invalidated]:
        """Kill."""
        return self._invalidate("a.status IN ('armed','confirmed')", (), reason, actor)

    def expire_due(self) -> list[str]:
        now = to_iso(self._now())
        with db.transaction(self.conn):
            rows = self.conn.execute(
                "SELECT a.id, o.cid FROM approvals a JOIN outbound_actions o ON o.id = a.action_id"
                " WHERE a.status IN ('pending','armed','confirmed') AND a.expires_at <= ?",
                (now,),
            ).fetchall()
            for r in rows:
                self.conn.execute("UPDATE approvals SET status = 'expired' WHERE id = ?", (r["id"],))
                self.audit.append("system", "approval_expired", {"approval_id": r["id"]}, r["cid"])
            self._kill_tokens([r["id"] for r in rows])
        return [r["id"] for r in rows]

    # ------------------------------------------------------------------ text edits
    def set_text_override(self, cid: str, *, title: str | None = None, body: str | None = None, actor: str) -> None:
        with db.transaction(self.conn):
            if title is not None:
                self.conn.execute(
                    "UPDATE contributions SET pr_title_override = ?, updated_at = ? WHERE id = ?",
                    (normalize_text(title, title=True), to_iso(self._now()), cid),
                )
            if body is not None:
                self.conn.execute(
                    "UPDATE contributions SET pr_body_override = ?, updated_at = ? WHERE id = ?",
                    (normalize_text(body), to_iso(self._now()), cid),
                )
            self.audit.append(actor, "pr_text_edited", {"title": title is not None, "body": body is not None}, cid)
            self.invalidate_contribution(cid, "text_edited", actor)

    # ---------------------------------------------------------------------- creation
    def create(self, action_id: str, *, tg_chat_id: int, body_parts_total: int, actor: str = "system") -> str:
        if body_parts_total < 1:
            raise ApprovalError("bad_parts")
        approval_id = new_approval_id()
        now = self._now()
        with db.transaction(self.conn):
            binding = compute_binding(self.conn, action_id, self.artifacts)
            ttl = timedelta(hours=self.limits().approval_ttl_hours)
            self.conn.execute(
                "INSERT INTO approvals (id, action_id, binding_json, binding_hash, dry_run_shown, status,"
                " tg_chat_id, body_parts_total, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    approval_id, action_id, canonical_json(binding).decode("utf-8"), binding_hash(binding),
                    int(binding["dry_run"]), "pending", tg_chat_id, body_parts_total, to_iso(now), to_iso(now + ttl),
                ),
            )  # fmt: skip
            self.audit.append(
                actor, "approval_created",
                {"approval_id": approval_id, "action_id": action_id, "binding_hash": binding_hash(binding)},
                binding["cid"],
            )  # fmt: skip
        return approval_id

    def set_card_message(self, approval_id: str, tg_message_id: int) -> None:
        with db.transaction(self.conn):
            self.conn.execute("UPDATE approvals SET tg_message_id = ? WHERE id = ?", (tg_message_id, approval_id))

    def mark_part_delivered(self, approval_id: str) -> None:
        with db.transaction(self.conn):
            self.conn.execute(
                "UPDATE approvals SET body_parts_delivered = MIN(body_parts_total, body_parts_delivered + 1)"
                " WHERE id = ?",
                (approval_id,),
            )

    def issue_card_tokens(self, approval_id: str) -> dict[str, str]:
        """Card buttons, bound to the card's message id. Refused until every body part landed."""
        with db.transaction(self.conn):
            row = self._approval(approval_id)
            if row["status"] != "pending":
                raise ApprovalError("not_pending", row["status"])
            if row["tg_message_id"] is None:
                raise ApprovalError("no_message")
            if row["body_parts_delivered"] < row["body_parts_total"]:
                raise ApprovalError("body_not_delivered", f"{row['body_parts_delivered']}/{row['body_parts_total']}")
            action = db.one(self.conn, "SELECT cid FROM outbound_actions WHERE id = ?", (row["action_id"],))
            assert action is not None
            expires = from_iso(row["expires_at"])
            return {
                purpose: self.mint_token(
                    purpose, chat_id=row["tg_chat_id"], message_id=row["tg_message_id"], expires_at=expires,
                    approval_id=approval_id, cid=action["cid"],
                )
                for purpose in CARD_PURPOSES
            }  # fmt: skip

    # ------------------------------------------------------------------------ tokens
    def mint_token(
        self,
        purpose: str,
        *,
        chat_id: int,
        message_id: int | None,
        expires_at: datetime,
        approval_id: str | None = None,
        cid: str | None = None,
        arg: str | None = None,
        single_use: bool = True,
    ) -> str:
        token = new_token()
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO ui_tokens (token, purpose, approval_id, cid, arg, tg_chat_id, tg_message_id,"
                " single_use, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    token,
                    purpose,
                    approval_id,
                    cid,
                    arg,
                    chat_id,
                    message_id,
                    int(single_use),
                    to_iso(expires_at),
                ),
            )
        return token

    def use_token(self, token: str, purposes: tuple[str, ...], *, chat_id: int, message_id: int) -> sqlite3.Row:
        """Validate and (if single-use) consume a callback token. Caller holds a transaction."""
        row = self.check_token(token, purposes, chat_id=chat_id, message_id=message_id)
        if row["single_use"]:
            self.conn.execute("UPDATE ui_tokens SET used_at = ? WHERE token = ?", (to_iso(self._now()), token))
        return row

    def check_token(self, token: str, purposes: tuple[str, ...], *, chat_id: int, message_id: int) -> sqlite3.Row:
        """Every token rule of §14.2 without consuming the token (read-only)."""
        if not TOKEN_RE.match(token):
            raise ApprovalError("token_invalid")
        row = db.one(self.conn, "SELECT * FROM ui_tokens WHERE token = ?", (token,))
        if row is None:
            raise ApprovalError("token_invalid")
        if row["purpose"] not in purposes:
            raise ApprovalError("token_purpose", row["purpose"])
        if row["used_at"] is not None:
            raise ApprovalError("token_used")
        if from_iso(row["expires_at"]) <= self._now():
            raise ApprovalError("token_expired")
        if row["tg_chat_id"] != chat_id or row["tg_message_id"] != message_id:
            raise ApprovalError("token_wrong_message")
        return row

    # --------------------------------------------------------------------- decisions
    def _recheck_binding(self, row: sqlite3.Row, actor: str) -> dict[str, Any]:
        try:
            binding = compute_binding(self.conn, row["action_id"], self.artifacts)
        except (ApprovalError, KeyError, ValueError) as exc:
            reason = f"binding_unavailable:{getattr(exc, 'code', type(exc).__name__)}"
            self._mark_invalid(row, reason, actor)
            raise Superseded("superseded", reason) from exc
        if binding_hash(binding) != row["binding_hash"]:
            self._mark_invalid(row, "binding_changed", actor)
            raise Superseded("superseded", "content or mode changed since you approved; please review again")
        return binding

    def _mark_invalid(self, row: sqlite3.Row, reason: str, actor: str) -> None:
        self.conn.execute(
            "UPDATE approvals SET status = 'invalidated', invalidated_reason = ? WHERE id = ?",
            (reason, row["id"]),
        )
        self._kill_tokens([row["id"]])
        self.audit.append(actor, "approval_invalidated", {"approval_id": row["id"], "reason": reason})

    def _check_live(self, row: sqlite3.Row, wanted: str) -> None:
        if row["status"] != wanted:
            raise ApprovalError("wrong_status", f"{row['status']} (expected {wanted})")
        if from_iso(row["expires_at"]) <= self._now():
            raise ApprovalError("approval_expired")

    def confirm_text(self, binding: Mapping[str, Any]) -> str:
        if binding["dry_run"]:
            return "Confirm: DRY RUN — nothing will be published. Expires in 5 min."
        profile = self.profile()
        what = {
            "open_pr": f"open PR on {binding['upstream']}",
            "push_update": f"push to PR #{binding.get('pr_number')} on {binding['upstream']}",
            "comment": f"comment on {binding['upstream']}#{binding.get('target_number')}",
            "issue_claim": f"claim issue {binding['upstream']}#{binding.get('issue_number')}",
        }[str(binding["kind"])]
        return f"Confirm: LIVE — {what} as {profile.github_login} (author {profile.commit_name}). Expires in 5 min."

    def approve(self, token: str, *, tg_user_id: int, chat_id: int, message_id: int) -> ArmResult:
        actor = f"tg:{tg_user_id}"
        result: ArmResult | None = None
        error: ApprovalError | None = None
        with db.transaction(self.conn):
            tok = self.use_token(token, ("approve",), chat_id=chat_id, message_id=message_id)
            row = self._approval(tok["approval_id"])
            try:
                self._check_live(row, "pending")
                binding = self._recheck_binding(row, actor)
            except ApprovalError as exc:
                error = exc
            else:
                now = self._now()
                confirm_expiry = now + timedelta(seconds=self.limits().confirm_ttl_seconds)
                self.conn.execute(
                    "UPDATE approvals SET status = 'armed', armed_at = ? WHERE id = ?",
                    (to_iso(now), row["id"]),
                )
                confirm = self.mint_token(
                    "confirm", chat_id=chat_id, message_id=message_id, expires_at=confirm_expiry,
                    approval_id=row["id"], cid=binding["cid"],
                )  # fmt: skip
                cancel = self.mint_token(
                    "cancel", chat_id=chat_id, message_id=message_id, expires_at=confirm_expiry,
                    approval_id=row["id"], cid=binding["cid"],
                )  # fmt: skip
                self.audit.append(actor, "approval_armed", {"approval_id": row["id"]}, binding["cid"])
                result = ArmResult(row["id"], confirm, cancel, self.confirm_text(binding), not binding["dry_run"])
        if error is not None:
            raise error
        assert result is not None
        return result

    def confirm(self, token: str, *, tg_user_id: int, chat_id: int, message_id: int) -> dict[str, Any]:
        actor = f"tg:{tg_user_id}"
        error: ApprovalError | None = None
        binding: dict[str, Any] = {}
        with db.transaction(self.conn):
            tok = self.use_token(token, ("confirm",), chat_id=chat_id, message_id=message_id)
            row = self._approval(tok["approval_id"])
            try:
                self._check_live(row, "armed")
                if not self.system_running():
                    raise ApprovalError("confirm_refused", "paused or killed")
                binding = self._recheck_binding(row, actor)
            except ApprovalError as exc:
                error = exc
            else:
                self.conn.execute(
                    "UPDATE approvals SET status = 'confirmed', confirmed_at = ?, decided_by_tg_user = ? WHERE id = ?",
                    (to_iso(self._now()), tg_user_id, row["id"]),
                )
                self.audit.append(
                    actor,
                    "approval_confirmed",
                    {"approval_id": row["id"], "dry_run": binding["dry_run"]},
                    binding["cid"],
                )
        if error is not None:
            raise error
        return binding

    def cancel(self, token: str, *, tg_user_id: int, chat_id: int, message_id: int) -> None:
        with db.transaction(self.conn):
            tok = self.use_token(token, ("cancel",), chat_id=chat_id, message_id=message_id)
            row = self._approval(tok["approval_id"])
            if row["status"] == "armed":
                self.conn.execute("UPDATE approvals SET status = 'pending', armed_at = NULL WHERE id = ?", (row["id"],))
                self.conn.execute(
                    "UPDATE ui_tokens SET used_at = ? WHERE approval_id = ? AND purpose = 'confirm'"
                    " AND used_at IS NULL",
                    (to_iso(self._now()), row["id"]),
                )
                self.audit.append(f"tg:{tg_user_id}", "approval_disarmed", {"approval_id": row["id"]})

    def reject(self, token: str, *, tg_user_id: int, chat_id: int, message_id: int, reason: str | None = None) -> str:
        with db.transaction(self.conn):
            tok = self.use_token(token, ("reject", "reject_reason"), chat_id=chat_id, message_id=message_id)
            row = self._approval(tok["approval_id"])
            if row["status"] not in OPEN_STATUSES:
                raise ApprovalError("wrong_status", row["status"])
            self.conn.execute(
                "UPDATE approvals SET status = 'rejected', decided_by_tg_user = ?, invalidated_reason = ? WHERE id = ?",
                (tg_user_id, reason, row["id"]),
            )
            self._kill_tokens([row["id"]])
            self.audit.append(f"tg:{tg_user_id}", "approval_rejected", {"approval_id": row["id"], "reason": reason})
            return str(row["action_id"])

    def consume(self, approval_id: str) -> dict[str, Any]:
        """Immediately before the first write: confirmed, unexpired, running, binding unchanged."""
        error: ApprovalError | None = None
        binding: dict[str, Any] = {}
        with db.transaction(self.conn):
            row = self._approval(approval_id)
            try:
                self._check_live(row, "confirmed")
                if not self.system_running():
                    raise ApprovalError("blocked", "paused or killed")
                binding = self._recheck_binding(row, "gatekeeper_internal")
            except ApprovalError as exc:
                error = exc
            else:
                self.conn.execute(
                    "UPDATE approvals SET status = 'consumed', consumed_at = ? WHERE id = ?",
                    (to_iso(self._now()), approval_id),
                )
                self._kill_tokens([approval_id])
                self.audit.append(
                    "gatekeeper_internal", "approval_consumed", {"approval_id": approval_id}, binding["cid"]
                )
        if error is not None:
            raise error
        return binding

    def invalidate_one(self, approval_id: str, reason: str, actor: str) -> None:
        """E.g. a live check failed at Confirm: this approval dies with its tokens."""
        with db.transaction(self.conn):
            row = self._approval(approval_id)
            if row["status"] in OPEN_STATUSES:
                self._mark_invalid(row, reason, actor)

    def rotate_card_tokens(self, approval_id: str) -> dict[str, str]:
        """After Cancel: every unused token of the card dies and a fresh card set is minted."""
        with db.transaction(self.conn):
            self._kill_tokens([approval_id])
            return self.issue_card_tokens(approval_id)

    def reissue(self, cid: str, *, tg_chat_id: int, actor: str, body_parts_total: int = 1) -> list[str]:
        """``/approve <cid>``: old approvals and their tokens die, fresh approvals are created (N4)."""
        with db.transaction(self.conn):
            self.invalidate_contribution(cid, "reissued", actor)
            actions = self.conn.execute(
                "SELECT id FROM outbound_actions WHERE cid = ? AND status IN ('pending_approval','approved')"
                " ORDER BY created_at",
                (cid,),
            ).fetchall()
            return [
                self.create(a["id"], tg_chat_id=tg_chat_id, body_parts_total=body_parts_total, actor=actor)
                for a in actions
            ]
