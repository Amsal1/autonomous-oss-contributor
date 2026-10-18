"""``/package`` verification and approval creation (DESIGN §12.5 steps 1-8, §12.5a, §9.2 N5, I6, I12).

Everything shown or bound comes from the gatekeeper's own per-rev mirror. The caller chooses
nothing about the action: ``push_update`` iff ``pr_number`` is set, else ``open_pr`` (M9).
Rejections are ``PackageRefused(422, code)``; ``patch_mismatch``/``patch_unreadable`` also fail
the contribution (redirected to WATCHING with a PR) and pause; ``branch_moved`` disables fixes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ossbot.common.clock import to_iso
from ossbot.common.config import resolve_target
from ossbot.common.gitcmd import CHANGE_REF
from ossbot.common.hashing import sha256_hex
from ossbot.common.ids import new_action_id
from ossbot.common.schemas import TERMINAL_STATES, OutboundKind, PackageArtifactName, PackageRequest, State
from ossbot.gatekeeper import db, derive, policy
from ossbot.gatekeeper.anomaly import Signal
from ossbot.gatekeeper.approvals import normalize_text
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.gitops import ArtifactError, GitError, GitOps
from ossbot.gatekeeper.state_machine import Actor

log = logging.getLogger(__name__)
GITLEAKS_BIN = "/usr/local/bin/gitleaks"
ACTION_MARKER_RE = re.compile(r"\n*<!-- ossbot:action=[a-z]_[0-9A-Z]{10} -->\s*$")
OPEN_ACTION_STATUSES = ("pending_approval", "approved", "executing")


class PackageRefused(RuntimeError):
    def __init__(self, status: int, code: str, detail: str = "") -> None:
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(f"{status} {code}{': ' + detail if detail else ''}")


GitleaksScanner = Callable[[bytes], list[str]]  # returns finding codes; empty = clean


def gitleaks_scan(data: bytes) -> list[str]:
    """``gitleaks stdin`` on the derived patch; fails closed when gitleaks is unusable."""
    try:
        proc = subprocess.run(
            [GITLEAKS_BIN, "stdin", "--no-banner", "--redact", "--log-level", "error", "--exit-code", "1"],
            input=data,
            capture_output=True,
            timeout=120,
            check=False,
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"gitleaks_unavailable:{type(exc).__name__}"]
    if proc.returncode == 0:
        return []
    if proc.returncode == 1:
        return ["gitleaks"]
    return [f"gitleaks_error:{proc.returncode}"]


@dataclass(frozen=True)
class PackageResult:
    status: int
    body: dict[str, Any]


def prune_retention(ctx: AppContext) -> list[tuple[str, int]]:
    """M13 retention: every per-rev mirror/derived/work copy of a terminal contribution goes."""
    removed: list[tuple[str, int]] = []
    mirrors = ctx.gitops.root / "mirrors"
    if not mirrors.is_dir():
        return removed
    terminal = {s.value for s in TERMINAL_STATES}
    for key_dir in mirrors.iterdir():
        for entry in key_dir.iterdir() if key_dir.is_dir() else ():
            match = re.fullmatch(r"(c_[0-9A-Z]{10})-r(\d+)\.git", entry.name)
            if match is None:
                continue
            row = db.one(ctx.conn, "SELECT upstream, state FROM contributions WHERE id = ?", (match.group(1),))
            if row is not None and row["state"] in terminal:
                ctx.gitops.delete_rev(row["upstream"], match.group(1), int(match.group(2)))
                removed.append((match.group(1), int(match.group(2))))
    return removed


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:limit].rstrip("-")) or "change"


def branch_name(cid: str, issue_number: int | None, title: str) -> str:
    return f"ossbot/{issue_number if issue_number else 'import'}-{slugify(title)}-{cid[-6:].lower()}"


class Packager:
    def __init__(self, ctx: AppContext, ops: GitOps, scanner: GitleaksScanner = gitleaks_scan) -> None:
        self.ctx = ctx
        self.ops = ops
        self.scanner = scanner

    # --------------------------------------------------------------------- helpers
    def _fail_contribution(self, cid: str, error_class: str, signal: Signal | None) -> None:
        self.ctx.state_machine.transition(
            cid, State.PACKAGING, State.FAILED, Actor.GATEKEEPER, {"error_class": error_class}
        )
        if signal is not None:
            self.ctx.anomaly.report(signal, f"/package {cid}", cid)

    def _approved_rev(self, cid: str) -> int | None:
        row = db.one(
            self.ctx.conn,
            "SELECT payload_json FROM outbound_actions WHERE cid = ? AND kind IN ('open_pr','push_update')"
            " AND status = 'done' ORDER BY executed_at DESC LIMIT 1",
            (cid,),
        )
        return int(json.loads(row["payload_json"])["rev"]) if row else None

    def _fork_repo(self, cid: str, upstream: str) -> str:
        row = db.one(
            self.ctx.conn,
            "SELECT result_json FROM outbound_actions WHERE cid = ? AND kind IN ('open_pr','push_update')"
            " AND status = 'done' ORDER BY executed_at DESC LIMIT 1",
            (cid,),
        )
        fork = json.loads(row["result_json"] or "{}").get("fork") if row else None
        return str(fork) if fork else f"{self.ctx.config.profile.github_login}/{upstream.split('/', 1)[1]}"

    def _repo_state(self, contrib: Any) -> policy.RepoState:
        vetting = json.loads(contrib["vetting_json"] or "{}")
        override = db.one(
            self.ctx.conn,
            "SELECT enabled, ai_policy_override FROM repo_overrides WHERE lower(upstream) = lower(?)",
            (contrib["upstream"],),
        )
        ai = vetting.get("ai_policy", "unknown")
        return policy.RepoState(
            ai_policy=ai if ai in {"allowed", "unclear", "banned", "unknown"} else "unknown",
            ai_override_allowed=bool(override and override["ai_policy_override"] == "allowed"),
            cla_required=bool(vetting.get("cla_required", False)),
            db_enabled=None if override is None or override["enabled"] is None else bool(override["enabled"]),
        )

    # ---------------------------------------------------------------------- package
    def package(self, cid: str, req: PackageRequest, actor: str) -> PackageResult:
        ctx = self.ctx
        contrib = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
        if contrib is None:
            raise PackageRefused(404, "not_found")
        if contrib["state"] != State.PACKAGING.value:
            raise PackageRefused(409, "not_packaging", contrib["state"])
        expected = self.ops.artifact_dir(cid, req.rev)
        if os.path.normpath(req.artifact_dir).rstrip("/") != str(expected):
            raise PackageRefused(400, "artifact_dir", "artifact_dir must be the gatekeeper-built directory")
        ctx.control.require_running("package")
        if contrib["artifact_rev"] is not None and req.rev < int(contrib["artifact_rev"]):
            raise PackageRefused(409, "stale_rev")
        kind: OutboundKind = "push_update" if contrib["pr_number"] is not None else "open_pr"
        upstream: str = contrib["upstream"]
        if contrib["base_sha"] is not None and (contrib["base_sha"], contrib["base_ref"]) != (
            req.base_sha,
            req.base_ref,
        ):
            raise PackageRefused(422, "base_mismatch")
        if kind == "open_pr" and (req.pr_title is None or req.pr_body is None):
            raise PackageRefused(400, "title_body_required")
        profile = ctx.config.profile

        # step 3 (R5-N1 first): private copy of the bundle, hash of the copy
        try:
            copy = self.ops.copy_bundle(cid, req.rev)
        except ArtifactError as exc:
            raise PackageRefused(422, "bundle_invalid", str(exc)) from exc
        if copy.sha256 != req.sha256s["change.bundle"]:
            raise PackageRefused(422, "bundle_sha256")
        side: dict[str, bytes | None] = {}
        names: tuple[PackageArtifactName, ...] = ("change.patch", "diffstat.json")
        for name in names:
            try:
                data: bytes | None = self.ops.read_side_artifact(cid, req.rev, name)
            except ArtifactError:
                data = None
            if data is not None and sha256_hex(data) != req.sha256s[name]:
                data = None  # changed after sandboxd hashed it: treated as unreadable (anomaly)
            side[name] = data

        # step 1: cache fetch, per-rev mirror with alternates
        try:
            tip = self.ops.fetch_upstream(upstream, req.base_ref)
        except (GitError, ValueError) as exc:
            raise PackageRefused(503, "upstream_fetch_failed", str(exc)) from exc
        mirror = self.ops.create_mirror(upstream, cid, req.rev, req.base_ref, tip)

        # step 2 / 12.5a: fork branch
        old_head: str | None = None
        fork_repo: str | None = None
        if kind == "push_update":
            old_head = contrib["head_sha"]
            fork_repo = self._fork_repo(cid, upstream)
            try:
                fork_tip = self.ops.fetch_fork(mirror, fork_repo, contrib["branch"])
            except (GitError, ValueError):
                fork_tip = None
            if old_head is None or fork_tip != old_head:
                self._fail_contribution(cid, "branch_moved", None)
                raise PackageRefused(422, "branch_moved")

        # step 3: list-heads / verify / fetch on the copy only
        try:
            heads = self.ops.list_heads(mirror, copy.path)
        except (GitError, ValueError) as exc:
            raise PackageRefused(422, "bundle_refs", str(exc)) from exc
        if len(heads) != 1 or heads[0][1] != CHANGE_REF:
            raise PackageRefused(422, "bundle_refs", f"{len(heads)} head(s)")
        if not self.ops.verify_bundle(mirror, copy.path):
            raise PackageRefused(422, "bundle_invalid", "prerequisite missing")
        try:
            self.ops.fetch_bundle(mirror, copy.path, CHANGE_REF, f"refs/ossbot/r{req.rev}")
        except GitError as exc:
            raise PackageRefused(422, "bundle_invalid", exc.stderr[-300:]) from exc
        head = self.ops.rev_parse(mirror, f"refs/ossbot/r{req.rev}")
        if head is None or head != heads[0][0]:
            raise PackageRefused(422, "bundle_refs", "fetched head differs from list-heads")

        # step 4 / 12.5a: history and identity
        base_sha = req.base_sha
        round_base = old_head if old_head is not None else base_sha
        if not self.ops.commit_exists(mirror, base_sha) or not self.ops.is_ancestor(
            mirror, base_sha, f"refs/upstream/{req.base_ref}"
        ):
            raise PackageRefused(422, "fabricated_base")
        if old_head is not None and not self.ops.is_ancestor(mirror, base_sha, old_head):
            raise PackageRefused(422, "fabricated_base", "base is not an ancestor of the PR head")
        if self.ops.rev_count(mirror, round_base, head) != 1:
            raise PackageRefused(422, "multi_commit")
        if self.ops.rev_parse(mirror, f"{head}^") != round_base:
            raise PackageRefused(422, "parent_not_base")
        derived = derive.derive(self.ops, mirror, round_base, head)
        identity = profile.identity
        for what, value in (
            ("author", derived.identity.author),
            ("committer", derived.identity.committer),
            ("signoff", derived.identity.signoff),
        ):
            if value != identity:
                raise PackageRefused(422, "identity_mismatch", what)
        if req.claimed_head_sha != head:
            raise PackageRefused(422, "claimed_head_mismatch")

        # step 5: store the derivation
        cumulative = None
        if kind == "push_update":
            cumulative = derive.derive(self.ops, mirror, base_sha, head).numstat_text
        derive.store(self.ops, cid, req.rev, derived, cumulative)

        # step 6: compare with the sandbox's outputs (never displayed)
        result = derive.compare(self.ops, derived, side["change.patch"], side["diffstat.json"])
        if result == "equivalent":
            log.info("sandbox patch differs only in non-content bytes", extra={"event": "patch_equivalent"})
        elif result in {"mismatch", "unreadable"}:
            error_class = f"patch_{result}"
            signal = Signal.PATCH_MISMATCH if result == "mismatch" else Signal.PATCH_UNREADABLE
            self._fail_contribution(cid, error_class, signal)
            raise PackageRefused(422, error_class)

        # step 7: policy on the derived diff, commit message, title, body
        title = normalize_text(req.pr_title, title=True) if req.pr_title is not None else None
        body = normalize_text(req.pr_body) if req.pr_body is not None else None
        eff_title = contrib["pr_title_override"] if contrib["pr_title_override"] is not None else title
        eff_body = contrib["pr_body_override"] if contrib["pr_body_override"] is not None else body
        message = derived.commit_message.decode("utf-8", "replace")
        confidentiality = policy.effective_confidentiality(ctx.conn, ctx.config)
        target = resolve_target(ctx.config.targets, upstream)
        violations = policy.check_diff(derived.entries, ctx.config.policy)
        values = ctx.secret_scanner.values  # U14: the gatekeeper's own secret values
        violations += policy.check_text(
            "commit_message", message, ctx.config, confidentiality, ctx.kiro_key_sha256, secret_values=values
        )
        if kind == "open_pr":
            violations += policy.check_text(
                "title", eff_title or "", ctx.config, confidentiality, ctx.kiro_key_sha256, secret_values=values
            )
            violations += policy.check_text(
                "body", eff_body or "", ctx.config, confidentiality, ctx.kiro_key_sha256, secret_values=values
            )
        violations += policy.check_repo(
            upstream, target, self._repo_state(contrib), policy.effective_forbidden_logins(ctx.conn, profile)
        )
        patch_text = derived.patch.decode("utf-8", "replace")
        if ctx.config.policy.secrets.kiro_key_hash_check and ctx.kiro_key_sha256:
            for candidate in policy.TOKENISH_RE.findall(patch_text):
                if sha256_hex(candidate.encode("utf-8")) in policy.kiro_key_hashes(ctx.kiro_key_sha256):
                    violations.append(policy.Violation("kiro_key", "patch"))
                    break
        if ctx.config.policy.secrets.gitleaks:
            violations += [policy.Violation("secret_pattern", hit) for hit in self.scanner(derived.patch)]
        if ctx.secret_scanner.value_hits(derived.patch):
            violations.append(policy.Violation("gatekeeper_secret", "patch"))
        if violations:
            codes = sorted({v.code for v in violations})
            if any(v.code in {"secret_pattern", "kiro_key", "gatekeeper_secret"} for v in violations):
                ctx.anomaly.report(Signal.SECRET_IN_TEXT, f"/package {cid}", cid)
            raise PackageRefused(422, "policy", ",".join(codes))

        # step 8: action + approval, or the N5 live claim first
        dry_run = db.current_dry_run(ctx.conn)
        branch = contrib["branch"] or branch_name(cid, contrib["issue_number"], eff_title or "change")
        now = to_iso(ctx.clock.now())
        payload: dict[str, Any] = {
            "rev": req.rev, "head_sha": head, "branch": branch, "fork_owner": profile.github_login,
            "identity": identity,
        }  # fmt: skip
        if kind == "push_update":
            payload.update({"pr_number": contrib["pr_number"], "old_head_sha": old_head, "fork_repo": fork_repo})
        vetting = json.loads(contrib["vetting_json"] or "{}")
        claim: tuple[PackageResult, str | None] | None = None
        approval_id = action_id = ""
        with db.transaction(ctx.conn):
            if contrib["artifact_rev"] is not None and req.rev > int(contrib["artifact_rev"]):
                ctx.approvals.invalidate_contribution(cid, "new_package_rev", actor)
                ctx.conn.execute(
                    "UPDATE outbound_actions SET status = 'cancelled' WHERE cid = ?"
                    " AND kind IN ('open_pr','push_update') AND status IN ('pending_approval','approved')",
                    (cid,),
                )
            ctx.conn.execute(
                "UPDATE contributions SET artifact_rev = ?, base_ref = ?, base_sha = ?, upstream_base_tip = ?,"
                " pr_title = COALESCE(?, pr_title), pr_body = COALESCE(?, pr_body), commit_message = ?,"
                " patch_sha256 = ?, commit_message_sha256 = ?, bundle_sha256 = ?, diffstat_json = ?,"
                " validation_json = ?, review_json = ?, branch = ?, updated_at = ? WHERE id = ?",
                (
                    req.rev, req.base_ref, base_sha, tip, title, body, message, derived.patch_sha256,
                    derived.commit_message_sha256, copy.sha256, json.dumps(derived.diffstat()),
                    json.dumps(req.validation), json.dumps(req.review), branch, now, cid,
                ),
            )  # fmt: skip
            ctx.audit.append(
                actor, "package_verified",
                {"rev": req.rev, "head": head, "kind": kind, "bundle_sha256": copy.sha256,
                 "patch_sha256": derived.patch_sha256, "comparison": result},
                cid,
            )  # fmt: skip
            if kind == "open_pr" and vetting.get("requires_claim_comment") and not dry_run:
                claim = self._live_claim(cid, actor, now)
            if claim is None:
                action_id = new_action_id()
                ctx.conn.execute(
                    "INSERT INTO outbound_actions (id, cid, kind, payload_json, status, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (action_id, cid, kind, json.dumps(payload, sort_keys=True), "pending_approval", now),
                )
                approval_id = ctx.create_approval(action_id, actor)
                ctx.state_machine.transition(
                    cid, State.PACKAGING, State.AWAITING_APPROVAL, Actor.GATEKEEPER, {"rev": req.rev}
                )
        keep = {req.rev, self._approved_rev(cid)}
        self.ops.prune(upstream, cid, lambda rev: rev in keep)
        if claim is not None:
            if claim[1] is not None:
                ctx.on_approval_created(claim[1])
            return claim[0]
        ctx.on_approval_created(approval_id)
        return PackageResult(200, {"kind": kind, "action_id": action_id, "approval_id": approval_id, "head_sha": head})

    def _live_claim(self, cid: str, actor: str, now: str) -> tuple[PackageResult, str | None] | None:
        """N5: with dry-run off a claim must be ``done`` before the open_pr action exists.

        Returns None when a live claim is done (go on with open_pr), else the 202 result and the
        id of a newly created claim approval (None when a claim is already open)."""
        ctx = self.ctx
        claims = ctx.conn.execute(
            "SELECT id, status, payload_json FROM outbound_actions WHERE cid = ? AND kind = 'issue_claim'"
            " ORDER BY created_at DESC",
            (cid,),
        ).fetchall()
        if any(c["status"] == "done" for c in claims):
            return None
        open_claim = next((c for c in claims if c["status"] in OPEN_ACTION_STATUSES), None)
        if open_claim is None:
            if not claims:
                raise PackageRefused(409, "claim_body_missing")
            previous = json.loads(claims[0]["payload_json"])
            claim_id = new_action_id()
            text = ACTION_MARKER_RE.sub("", previous["body"]).rstrip("\n") + f"\n\n<!-- ossbot:action={claim_id} -->"
            ctx.conn.execute(
                "INSERT INTO outbound_actions (id, cid, kind, payload_json, status, created_at) VALUES (?,?,?,?,?,?)",
                (claim_id, cid, "issue_claim", json.dumps({"target_number": previous["target_number"], "body": text}),
                 "pending_approval", now),
            )  # fmt: skip
            approval_id = ctx.create_approval(claim_id, actor)
            ctx.conn.execute(
                "UPDATE contributions SET state_reason = 'awaiting_live_claim', updated_at = ? WHERE id = ?", (now, cid)
            )
            ctx.audit.append(actor, "live_claim_required", {"action_id": claim_id}, cid)
            return PackageResult(202, {"state_reason": "awaiting_live_claim", "action_id": claim_id}), approval_id
        return PackageResult(202, {"state_reason": "awaiting_live_claim", "action_id": open_claim["id"]}), None
