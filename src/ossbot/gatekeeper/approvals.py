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
from ossbot.common.schemas import OutboundKind, State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.audit import AuditLog
from ossbot.gatekeeper.telegram import cards, keyboards

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
# Delta 3 (DESIGN §14.18.4.1): decision purposes are single-use; the card's view purposes are
# multi-use, expire with the approval and die with it (``_kill_tokens``). View handlers call
# ``check_token``, never ``use_token``. ``edit_title``/``edit_body``/``clear_edits`` moved behind
# the S7 picker as kit tokens (PLAN item 45).
DECISION_PURPOSES = ("approve", "reject", "reject_reason", "changes")
# Shown on the S12 stale card and in toasts (§14.11.12); keys are invalidated_reason values.
REASON_LABELS: dict[str, str] = {
    "config_snapshot_changed": "config reloaded",
    "dry_run_toggled": "dry-run changed",
    "killed": "killed",
    "reissued": "re-sent",
    "text_edited": "text edited",
    "new_package_rev": "new revision",
    "binding_changed": "content or mode changed",
    "tier_changed": "Hermes tier changed",
    "new_proposal_version": "new proposal version",
    "new_skill_version": "new skill version",
    "render_failed": "card could not be rendered",
    "egress_anomaly": "egress anomaly: contribution held",  # open-discovery design §13.3 (holds.py)
    "egress_floor": "floor reach attempt: contribution held",  # ADR-0025 (holds.py)
}
# Eligibility of a re-send (§14.18.4.3): the contribution states each kind lives in.
REISSUE_STATES: dict[str, frozenset[str]] = {
    "open_pr": frozenset({"AWAITING_APPROVAL"}),
    "push_update": frozenset({"AWAITING_APPROVAL"}),
    "comment": frozenset({"VETTED", "PACKAGING", "WATCHING", "AWAITING_APPROVAL"}),
    "issue_claim": frozenset({"VETTED", "PACKAGING", "WATCHING", "AWAITING_APPROVAL"}),
}


def card_purposes(kind: OutboundKind, has_interdiff: bool) -> tuple[str, ...]:
    """The tokens one PR-family card carries (§14.18.4.1)."""
    if kind == "open_pr":
        return (*DECISION_PURPOSES, "explore", "interdiff" if has_interdiff else "files", "edit_text_pick")
    if kind == "push_update":
        return (*DECISION_PURPOSES, "explore", "files")
    return ("approve", "reject", "reject_reason", "edit_text_pick")  # comment, issue_claim


def reissue_eligible(kind: OutboundKind, action_status: str, state: State) -> bool:
    """One predicate for reissue, stale_retry, typed /approve, Show current and comment_replace."""
    return action_status == "pending_approval" and state.value in REISSUE_STATES.get(kind, frozenset())


def _state_of(value: str) -> State | None:
    try:
        return State(value)
    except ValueError:
        return None


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
    """``action_id`` is None for proposal and skill approvals, ``cid`` None for skills (§14.18.4.2)."""

    approval_id: str
    action_id: str | None
    cid: str | None


@dataclass(frozen=True)
class ArmResult:
    approval_id: str
    confirm_token: str
    cancel_token: str
    confirm_text: str
    live: bool


# Called inside disarm_expired's transaction with (approval_id, armed_at, fresh card tokens) so the
# S1 edit is enqueued atomically with the disarm (CardPublisher.render_s1, delta 2).
OnDisarmed = Callable[[str, str, dict[str, str]], None]


def _no_disarm_hook(approval_id: str, armed_at: str, tokens: dict[str, str]) -> None:
    return None


class ApprovalService:
    id_prefix = "a"

    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        audit: AuditLog,
        control_dir: Path,
        limits: Callable[[], Limits],
        profile: Callable[[], ProfileConfig],
        artifacts: ArtifactSource | None = None,
        interdiff: Callable[[str, int], bool] | None = None,
    ) -> None:
        self.conn = conn
        self.clock = clock
        self.audit = audit
        self.control_dir = control_dir
        self.limits = limits
        self.profile = profile
        self.artifacts: ArtifactSource = artifacts or NoArtifacts()
        # derived/<cid>/<rev>/interdiff.json present and usable (the interdiff ships in PLAN item 45, FEAT-009)
        self.interdiff: Callable[[str, int], bool] = interdiff or (lambda cid, rev: False)
        self.on_disarmed: OnDisarmed = _no_disarm_hook
        # (kind, text) -> policy violations (policy.check_text with the current snapshot, wired by
        # build_context); objects with a ``code`` attribute
        self.text_check: Callable[[str, str], list[Any]] | None = None
        # action id -> the review mirror PR URL for card row 4 (``ReviewMirror.card_url``, wired by main)
        self.mirror_url: Callable[[str], str | None] = lambda action_id: None

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

    def disarm_expired(self) -> list[str]:
        """Delta 2: an armed approval whose Confirm window passed goes back to pending with a fresh
        card (S1). The unused confirm/cancel tokens die, the card set is re-minted, the S1 edit is
        enqueued through ``on_disarmed`` in the same transaction."""
        cutoff = to_iso(self._now() - timedelta(seconds=self.limits().confirm_ttl_seconds))
        done: list[str] = []
        with db.transaction(self.conn):
            rows = self.conn.execute(
                "SELECT a.id, a.armed_at, o.cid FROM approvals a JOIN outbound_actions o ON o.id = a.action_id"
                " WHERE a.status = 'armed' AND a.armed_at <= ?",
                (cutoff,),
            ).fetchall()
            for r in rows:
                self.conn.execute("UPDATE approvals SET status = 'pending', armed_at = NULL WHERE id = ?", (r["id"],))
                tokens = self.rotate_card_tokens(r["id"])  # kills confirm/cancel and the old card set
                self.audit.append(
                    "system", "approval_disarmed", {"approval_id": r["id"], "reason": "confirm_expired"}, r["cid"]
                )
                self.on_disarmed(r["id"], str(r["armed_at"]), tokens)
                done.append(str(r["id"]))
        return done

    # ------------------------------------------------------------------ text edits
    def set_text_override(self, cid: str, *, title: str | None = None, body: str | None = None, actor: str) -> None:
        """Store a human title/body edit (the S7 Title/Body ForceReply, PLAN item 45). P1-4: the
        normalized text runs ``policy.check_text`` (title one line of 1-256, body <= 60 000, disclosure,
        confidentiality floor, secret patterns and values) and any violation raises
        ``ApprovalError("text_refused", codes)`` before anything is stored. No check wired = refused."""
        checks: list[tuple[str, str]] = []
        if title is not None:
            checks.append(("title", normalize_text(title, title=True)))
        if body is not None:
            checks.append(("body", normalize_text(body)))
        if self.text_check is None:
            raise ApprovalError("text_refused", "no text policy available")
        codes = sorted({f"{kind}:{v.code}" for kind, text in checks for v in self.text_check(kind, text)})
        if codes:
            raise ApprovalError("text_refused", ",".join(codes))
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
    def create(
        self,
        action_id: str,
        *,
        tg_chat_id: int,
        body_parts_total: int,
        actor: str = "system",
        expires_at: datetime | None = None,
    ) -> str:
        """A given ``expires_at`` (a re-send keeping the window, delta 7) replaces now + TTL."""
        if body_parts_total < 1:
            raise ApprovalError("bad_parts")
        approval_id = new_approval_id()
        now = self._now()
        if expires_at is not None and expires_at <= now:
            raise ApprovalError("bad_expiry", to_iso(expires_at))
        with db.transaction(self.conn):
            binding = compute_binding(self.conn, action_id, self.artifacts)
            expiry = expires_at or now + timedelta(hours=self.limits().approval_ttl_hours)
            self.conn.execute(
                "INSERT INTO approvals (id, action_id, binding_json, binding_hash, dry_run_shown, status,"
                " tg_chat_id, body_parts_total, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    approval_id, action_id, canonical_json(binding).decode("utf-8"), binding_hash(binding),
                    int(binding["dry_run"]), "pending", tg_chat_id, body_parts_total, to_iso(now), to_iso(expiry),
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
            action = db.one(
                self.conn, "SELECT cid, kind, payload_json FROM outbound_actions WHERE id = ?", (row["action_id"],)
            )
            assert action is not None
            expires = from_iso(row["expires_at"])
            rev = json.loads(action["payload_json"]).get("rev")
            has_interdiff = isinstance(rev, int) and self.interdiff(action["cid"], rev)
            return {
                purpose: self.mint_token(
                    purpose, chat_id=row["tg_chat_id"], message_id=row["tg_message_id"], expires_at=expires,
                    approval_id=approval_id, cid=action["cid"], single_use=purpose in DECISION_PURPOSES,
                )
                for purpose in card_purposes(action["kind"], has_interdiff)
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
            return self.reject_open(str(tok["approval_id"]), tg_user_id=tg_user_id, reason=reason)

    def reject_open(self, approval_id: str, *, tg_user_id: int, reason: str | None = None) -> str:
        """Reject an open approval by id: the card's ❌ path after its token, or ``/reject`` after its
        own message-bound confirm token (§14.14.1). Returns the action id."""
        with db.transaction(self.conn):
            row = self._approval(approval_id)
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

    def reissue(
        self,
        cid: str,
        *,
        tg_chat_id: int,
        actor: str,
        part_count: Callable[[str], int],
        keep_expiry: bool = False,
    ) -> list[str]:
        """Re-send every open card of ``cid`` (N4, delta 7, §14.18.4.3). Only ``AppContext.reissue``
        calls this (it publishes after the commit). In one transaction: refuse while a sibling is
        ``confirmed`` (``submitting``); pick the eligible actions (else ``nothing_to_resend``); read
        each newest expiry before invalidating; invalidate pending/armed with ``reissued``; create
        one approval per eligible action with its own part count (``part_count(action_id)``)."""
        now = self._now()
        keep_floor = now + timedelta(seconds=self.limits().confirm_ttl_seconds)
        with db.transaction(self.conn):
            contrib = db.one(self.conn, "SELECT state FROM contributions WHERE id = ?", (cid,))
            if contrib is None:
                raise ApprovalError("unknown_contribution", cid)
            confirmed = db.one(
                self.conn,
                "SELECT a.id FROM approvals a JOIN outbound_actions o ON o.id = a.action_id"
                " WHERE o.cid = ? AND a.status = 'confirmed' LIMIT 1",
                (cid,),
            )
            if confirmed is not None:
                raise ApprovalError("submitting", str(confirmed["id"]))
            state = _state_of(str(contrib["state"]))
            actions = [
                a
                for a in self.conn.execute(
                    "SELECT id, kind, status FROM outbound_actions WHERE cid = ? ORDER BY created_at", (cid,)
                ).fetchall()
                if state is not None and reissue_eligible(a["kind"], a["status"], state)
            ]
            if not actions:
                raise ApprovalError("nothing_to_resend", str(contrib["state"]))
            inherited: dict[str, datetime | None] = {}
            for a in actions:
                newest = db.one(
                    self.conn,
                    "SELECT expires_at FROM approvals WHERE action_id = ? ORDER BY expires_at DESC LIMIT 1",
                    (a["id"],),
                )
                previous = from_iso(newest["expires_at"]) if newest is not None else None
                keep = keep_expiry and previous is not None and previous > keep_floor
                inherited[a["id"]] = previous if keep else None
            ids = [a["id"] for a in actions]
            marks = ",".join("?" * len(ids))
            self._invalidate(f"a.status IN ('pending','armed') AND a.action_id IN ({marks})", tuple(ids), "reissued",
                             actor)  # fmt: skip
            return [
                self.create(
                    a["id"], tg_chat_id=tg_chat_id, body_parts_total=part_count(a["id"]), actor=actor,
                    expires_at=inherited[a["id"]],
                )
                for a in actions
            ]  # fmt: skip

    # ------------------------------------------------------- card plumbing (§14.18.4.4)
    def _action_of(self, approval_id: str) -> sqlite3.Row:
        row = db.one(
            self.conn,
            "SELECT o.id, o.kind FROM approvals a JOIN outbound_actions o ON o.id = a.action_id WHERE a.id = ?",
            (approval_id,),
        )
        if row is None:
            raise ApprovalError("unknown_approval", approval_id)
        return row

    def card_view(self, approval_id: str) -> cards.CardMessages:
        return cards.render(cards.view_for_approval(self.conn, approval_id))

    def card_rows(self, approval_id: str, tokens: dict[str, str]) -> keyboards.Rows:
        action = self._action_of(approval_id)
        return keyboards.card_rows(action["kind"], tokens, files=cards.file_count(self.conn, str(action["id"])),
                                   mirror_url=self.mirror_url(str(action["id"])))  # fmt: skip

    def part_count(self, approval_id: str) -> int:
        return cards.part_count(self.conn, str(self._action_of(approval_id)["id"]))
