"""Submission after Confirm (DESIGN §12.5 "Submission", §12.5a, M2, M5, R4-N2, I1, I4).

0. One SQLite transaction: approval ``consumed`` (binding recomputed and equal), action
   ``executing``, contribution ``AWAITING_APPROVAL→APPROVED→SUBMITTING``. Nothing is written
   to GitHub before this commit.
1. Immutable snapshot of every byte to publish, re-hashed against the approved binding.
2. Token identity: ``GET /user`` login == ``profile.github_login`` and not forbidden (else kill);
   ``X-OAuth-Scopes`` has ``public_repo``.
3. Upstream ancestry via ``refs/upstream-live`` and the per-kind live checks (§12.6).
4-6. Each write (fork, push, PR, comment) starts with ``control_checkpoint(snapshot)``: running
   and the dry-run flag unchanged. In dry-run the write is replaced by a recorded "would" payload.
A failed checkpoint stops before the next write: action ``failed``/``interrupted`` with the writes
already done, contribution ``SUBMIT_FAILED``, a Retry note (re-approval required). Writes are never
resumed automatically. No ``GITHUB_WRITE_TOKEN``: refused before step 0 (``no_write_token``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from ossbot.common.clock import to_iso
from ossbot.common.control_flags import Blocked
from ossbot.common.hashing import sha256_text
from ossbot.common.schemas import OutboundKind, State
from ossbot.gatekeeper import db, live_checks, policy, signing
from ossbot.gatekeeper.anomaly import Signal
from ossbot.gatekeeper.approvals import ApprovalError
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.control import ModeChanged
from ossbot.gatekeeper.github_read import GitHubError, GitHubReader
from ossbot.gatekeeper.github_writer import GitHubWriter, GitPusher, Pusher
from ossbot.gatekeeper.state_machine import Actor

log = logging.getLogger(__name__)
RETRY_DELAYS: tuple[float, ...] = (2.0, 8.0, 30.0)
FORK_POLL_SECONDS = 3.0
FORK_POLL_MAX_SECONDS = 300.0
PR_KINDS: frozenset[str] = frozenset({"open_pr", "push_update"})
Cause = Literal["pause", "kill", "mode_changed", "held"]  # held: an egress-anomaly hold (open-discovery §13.3)
HELD_TEXT = (
    "Submission interrupted by an egress-anomaly hold after {done}; nothing further was written. Resume the"
    " hold first, then [Retry]."
)
Sleep = Callable[[float], Awaitable[None]]
RetryNote = Callable[[str, str], None]  # (failed action id, text)


class Interrupted(Exception):
    def __init__(self, cause: Cause) -> None:
        super().__init__(cause)
        self.cause: Cause = cause


class SubmitFailed(Exception):
    def __init__(self, error_class: str, detail: str = "") -> None:
        super().__init__(f"{error_class}: {detail}" if detail else error_class)
        self.error_class = error_class


@dataclass(frozen=True)
class Snapshot:
    approval_id: str
    action_id: str
    cid: str
    kind: OutboundKind
    upstream: str
    dry_run: bool
    policy_hash: str
    binding_hash: str
    decided_by: str
    head: str | None = None
    branch: str | None = None
    rev: int | None = None
    fork_owner: str | None = None
    base_ref: str | None = None
    base_sha: str | None = None
    title: str | None = None
    body: str | None = None
    pr_number: int | None = None
    old_head_sha: str | None = None
    fork_repo: str | None = None
    issue_number: int | None = None
    target_number: int | None = None
    since: str | None = None
    unsigned_refusal: str | None = None  # an unsigned rev for a repo that requires signed commits


@dataclass
class Progress:
    writes: list[str] = field(default_factory=list)
    would: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)


class Submitter:
    def __init__(
        self,
        ctx: AppContext,
        client: httpx.AsyncClient,
        pusher: Pusher,
        *,
        token: str | None,
        sleep: Sleep = asyncio.sleep,
        retry_note: RetryNote | None = None,
    ) -> None:
        self.ctx = ctx
        self.reader = GitHubReader(client)
        self.writer = GitHubWriter(client, pusher)
        self.token = token
        self.sleep = sleep
        self.retry_note: RetryNote = retry_note or (lambda action_id, text: ctx.notify(text))

    # ----------------------------------------------------------------- entry
    async def submit(self, approval_id: str) -> str:
        """Run the submission of a confirmed approval. Returns the final action status."""
        if not self.token:
            return self._refuse_no_token(approval_id)
        snap = self._step0(approval_id)
        progress = Progress()
        try:
            await self._run(snap, progress)
        except Interrupted as exc:
            return self._interrupted(snap, progress, exc.cause)
        except SubmitFailed as exc:
            return self._failed(snap, progress, exc.error_class, str(exc))
        return self._succeeded(snap, progress)

    async def preflight(self, approval_id: str) -> list[str]:
        """Confirm-time live checks (§12.4, §12.6): GitHub reads and the upstream-live ancestry only."""
        if not self.token:
            return []  # submit() refuses with no_write_token without consuming anything
        ctx = self.ctx
        row = db.one(
            ctx.conn,
            "SELECT o.id AS action_id, o.kind, o.payload_json, c.* FROM approvals a"
            " JOIN outbound_actions o ON o.id = a.action_id JOIN contributions c ON c.id = o.cid WHERE a.id = ?",
            (approval_id,),
        )
        if row is None:
            return ["unknown_approval"]
        payload: dict[str, Any] = json.loads(row["payload_json"])
        vetting = json.loads(row["vetting_json"] or "{}")
        kind: OutboundKind = row["kind"]
        inp = live_checks.LiveInput(
            kind=kind, upstream=row["upstream"], login=ctx.config.profile.github_login,
            issue_number=row["issue_number"], pr_number=payload.get("pr_number", row["pr_number"]),
            branch=payload.get("branch"), target_number=payload.get("target_number"),
            old_head_sha=payload.get("old_head_sha"), fork_repo=payload.get("fork_repo"),
            since=vetting.get("vetted_at") or row["created_at"], action_id=row["action_id"],
        )  # fmt: skip
        failed: list[str] = []
        if kind in PR_KINDS and signing.unsigned_refusal(row["upstream"], vetting, payload) is not None:
            failed.append("unsigned_commit")  # ADR-0006 addendum: the repo requires signed commits
        try:
            if kind == "open_pr":
                mirror = ctx.gitops.mirror_dir(row["upstream"], row["id"], int(payload["rev"]))
                failed += await asyncio.to_thread(
                    live_checks.upstream_ancestry, ctx.gitops, row["upstream"], mirror, row["base_ref"], row["base_sha"]
                )
            failed += await live_checks.run(
                inp, self.reader, conn=ctx.conn, clock=ctx.clock, limits=ctx.config.policy.limits
            )
        except GitHubError as exc:
            failed.append(f"github_unavailable:{exc.status or 'network'}")
        return failed

    def _refuse_no_token(self, approval_id: str) -> str:
        ctx = self.ctx
        with db.transaction(ctx.conn):
            row = db.one(ctx.conn, "SELECT action_id, status FROM approvals WHERE id = ?", (approval_id,))
            if row is None:
                raise ApprovalError("unknown_approval", approval_id)
            ctx.conn.execute(
                "UPDATE approvals SET status = 'invalidated', invalidated_reason = 'no_write_token' WHERE id = ?",
                (approval_id,),
            )
            ctx.conn.execute(
                "UPDATE ui_tokens SET used_at = ? WHERE approval_id = ? AND used_at IS NULL",
                (to_iso(ctx.clock.now()), approval_id),
            )
            ctx.conn.execute(
                "UPDATE outbound_actions SET error_class = 'no_write_token' WHERE id = ?", (row["action_id"],)
            )
            ctx.audit.append(
                "gatekeeper_internal", "submit_refused", {"approval_id": approval_id, "reason": "no_write_token"}
            )
        log.warning("submit refused: no GITHUB_WRITE_TOKEN", extra={"event": "submit_refused"})
        ctx.notify(
            "⛔ Submit refused: no GitHub write token is configured (error_class=no_write_token)."
            " Nothing was published."
        )
        return "refused"

    # ----------------------------------------------------------------- step 0/1
    def _step0(self, approval_id: str) -> Snapshot:
        ctx = self.ctx
        error: Exception | None = None
        snap: Snapshot | None = None
        with db.transaction(ctx.conn):
            try:
                binding = ctx.approvals.consume(approval_id)
                appr = db.one(ctx.conn, "SELECT * FROM approvals WHERE id = ?", (approval_id,))
                assert appr is not None
                action = db.one(ctx.conn, "SELECT * FROM outbound_actions WHERE id = ?", (appr["action_id"],))
                assert action is not None
                if action["status"] != "pending_approval":
                    raise ApprovalError("action_not_pending", action["status"])
                contrib = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (action["cid"],))
                assert contrib is not None
                snap = self._snapshot(binding, appr, action, contrib)
                ctx.conn.execute(
                    "UPDATE outbound_actions SET status = 'executing', executed_at = ? WHERE id = ?",
                    (to_iso(ctx.clock.now()), action["id"]),
                )
                if snap.kind in PR_KINDS:
                    ctx.state_machine.transition(
                        snap.cid, State.AWAITING_APPROVAL, State.APPROVED, _human(), {}, snap.decided_by
                    )
                    ctx.state_machine.transition(snap.cid, State.APPROVED, State.SUBMITTING, _internal(), {})
                ctx.audit.append(
                    "gatekeeper_internal", "submit_started",
                    {"action_id": snap.action_id, "kind": snap.kind, "dry_run": snap.dry_run}, snap.cid,
                )  # fmt: skip
            except ApprovalError as exc:
                error = exc
        if error is not None:
            raise error
        assert snap is not None
        return snap

    def _snapshot(self, binding: dict[str, Any], appr: Any, action: Any, contrib: Any) -> Snapshot:
        """Step 1: every later step uses only these bytes; they must hash to the approved binding."""
        payload: dict[str, Any] = json.loads(action["payload_json"])
        kind: OutboundKind = action["kind"]
        common = {
            "approval_id": appr["id"], "action_id": action["id"], "cid": contrib["id"], "kind": kind,
            "upstream": contrib["upstream"], "dry_run": bool(binding["dry_run"]), "policy_hash": binding["policy_hash"],
            "binding_hash": appr["binding_hash"], "decided_by": f"tg:{appr['decided_by_tg_user']}",
            "issue_number": contrib["issue_number"], "pr_number": contrib["pr_number"],
        }  # fmt: skip
        if kind in {"comment", "issue_claim"}:
            body = str(payload["body"])
            if sha256_text(body) != binding["body_sha256"]:
                raise ApprovalError("snapshot_mismatch", "body")
            vetting = json.loads(contrib["vetting_json"] or "{}")
            return Snapshot(
                **common, body=body, target_number=int(payload["target_number"]),
                since=vetting.get("vetted_at") or contrib["created_at"],
            )  # fmt: skip
        common["unsigned_refusal"] = signing.unsigned_refusal(
            contrib["upstream"], json.loads(contrib["vetting_json"] or "{}"), payload
        )
        if kind == "open_pr":
            title = contrib["pr_title_override"] if contrib["pr_title_override"] is not None else contrib["pr_title"]
            body = contrib["pr_body_override"] if contrib["pr_body_override"] is not None else contrib["pr_body"]
            if sha256_text(title or "") != binding["title_sha256"] or sha256_text(body or "") != binding["body_sha256"]:
                raise ApprovalError("snapshot_mismatch", "title/body")
            return Snapshot(
                **common, head=payload["head_sha"], branch=payload["branch"], rev=int(payload["rev"]),
                fork_owner=payload["fork_owner"], base_ref=contrib["base_ref"], base_sha=contrib["base_sha"],
                title=title, body=body,
            )  # fmt: skip
        return Snapshot(
            **{**common, "pr_number": int(payload["pr_number"])}, head=payload["head_sha"], branch=payload["branch"],
            rev=int(payload["rev"]), fork_owner=payload["fork_owner"], base_ref=contrib["base_ref"],
            base_sha=contrib["base_sha"], old_head_sha=payload["old_head_sha"], fork_repo=payload.get("fork_repo"),
        )  # fmt: skip

    # ---------------------------------------------------------------- steps 2-6
    def _checkpoint(self, what: str, snap: Snapshot) -> None:
        try:
            self.ctx.control.checkpoint(f"submit {what}", snap.dry_run)
        except Blocked as exc:
            raise Interrupted("kill" if exc.flags.killed is not None else "pause") from exc
        except ModeChanged as exc:
            raise Interrupted("mode_changed") from exc
        except RuntimeError as exc:  # control row says paused/killed without a flag (N12)
            raise Interrupted("pause") from exc
        if self.ctx.holds.is_held(snap.cid):  # read-only; a hold stops before the next write like a kill
            raise Interrupted("held")

    async def _identity(self) -> None:
        ctx = self.ctx
        profile = ctx.config.profile
        try:
            ident = await self.reader.identity()
        except GitHubError as exc:
            if exc.auth:
                ctx.anomaly.report(Signal.GITHUB_WRITE_AUTH, "GET /user rejected the write token")
            raise SubmitFailed("identity_unavailable", str(exc)) from exc
        forbidden = policy.effective_forbidden_logins(ctx.conn, profile)
        if ident.login.lower() != profile.github_login.lower() or ident.login.lower() in forbidden:
            ctx.anomaly.report(Signal.TOKEN_LOGIN_MISMATCH, f"token login is {ident.login!r}")
            raise SubmitFailed("token_login_mismatch")
        if "public_repo" not in ident.scopes and "repo" not in ident.scopes:
            ctx.anomaly.report(Signal.GITHUB_WRITE_AUTH, "write token lacks public_repo")
            raise SubmitFailed("token_scope")

    async def _live(self, snap: Snapshot) -> None:
        ctx = self.ctx
        failed: list[str] = []
        if snap.kind == "open_pr":
            assert snap.base_ref
            assert snap.base_sha
            assert snap.rev
            mirror = ctx.gitops.mirror_dir(snap.upstream, snap.cid, snap.rev)
            failed += await asyncio.to_thread(
                live_checks.upstream_ancestry, ctx.gitops, snap.upstream, mirror, snap.base_ref, snap.base_sha
            )
        try:
            failed += await live_checks.run(
                live_input(snap, ctx.config.profile.github_login), self.reader,
                conn=ctx.conn, clock=ctx.clock, limits=ctx.config.policy.limits,
            )  # fmt: skip
        except GitHubError as exc:
            raise SubmitFailed("live_check_unavailable", str(exc)) from exc
        if failed:
            if snap.kind == "push_update" and "branch_moved" in failed:
                raise SubmitFailed("branch_moved")
            raise SubmitFailed("live_check", ",".join(failed))

    async def _write[T](
        self, what: str, snap: Snapshot, progress: Progress, would: dict[str, Any], fn: Callable[[], Awaitable[T]]
    ) -> T | None:
        """One outbound write: checkpoint before every attempt; network/5xx retried 2/8/30 s."""
        for attempt in range(len(RETRY_DELAYS) + 1):
            self._checkpoint(what, snap)
            if snap.dry_run:
                progress.would[what] = would
                return None
            try:
                result = await fn()
            except GitHubError as exc:
                if exc.auth:
                    self.ctx.anomaly.report(Signal.GITHUB_WRITE_AUTH, f"{what}: HTTP {exc.status}")
                    raise SubmitFailed("github_auth", what) from exc
                if not exc.transient or attempt == len(RETRY_DELAYS):
                    raise SubmitFailed(f"{what}_failed", str(exc)) from exc
                await self.sleep(RETRY_DELAYS[attempt])
                continue
            progress.writes.append(what)
            with db.transaction(self.ctx.conn):
                self.ctx.audit.append(
                    "gatekeeper_internal", "outbound_write", {"action_id": snap.action_id, "write": what}, snap.cid
                )
                self.ctx.conn.execute(
                    "UPDATE outbound_actions SET result_json = ? WHERE id = ?",
                    (json.dumps({"writes": progress.writes, **progress.result}), snap.action_id),
                )
            self.ctx.anchors.trigger()
            return result
        raise SubmitFailed(f"{what}_failed")  # pragma: no cover - loop always returns or raises

    async def _run(self, snap: Snapshot, progress: Progress) -> None:
        if snap.unsigned_refusal is not None:  # before any read or write (ADR-0006 addendum)
            self.ctx.notify(snap.unsigned_refusal)
            raise SubmitFailed("unsigned_commit", "the repo requires signed commits")
        await self._identity()
        await self._live(snap)
        login = self.ctx.config.profile.github_login
        if snap.kind == "open_pr":
            await self._open_pr(snap, progress, login)
        elif snap.kind == "push_update":
            await self._push_update(snap, progress)
        else:
            assert snap.target_number is not None
            assert snap.body is not None
            number, body = snap.target_number, snap.body

            async def post() -> dict[str, Any]:
                return await self.writer.create_comment(snap.upstream, number, body)

            doc = await self._write(
                "post_comment", snap, progress, {"upstream": snap.upstream, "number": number, "body": body}, post
            )
            if doc is not None:
                progress.result["comment_url"] = doc.get("html_url")

    async def _fork(self, snap: Snapshot, progress: Progress, login: str) -> str:
        name = snap.upstream.split("/", 1)[1]
        if snap.dry_run:
            self._checkpoint("fork", snap)
            progress.would["fork"] = {"upstream": snap.upstream, "owner": login, "name": name}
            return f"{login}/{name}"
        for candidate in (name, f"{name}-ossbot"):
            full = f"{login}/{candidate}"
            try:
                repo = await self.reader.repo(full)
            except GitHubError as exc:
                raise SubmitFailed("fork_failed", str(exc)) from exc
            if repo is not None:
                parent = str((repo.get("parent") or {}).get("full_name", ""))
                if repo.get("fork") and parent.lower() == snap.upstream.lower():
                    self._checkpoint("fork", snap)  # reuse: no write, but the step still starts here
                    return full
                continue  # the name is taken by something that is not our fork

            async def create(cand: str = candidate) -> dict[str, Any]:
                return await self.writer.create_fork(snap.upstream, cand)

            await self._write("fork", snap, progress, {}, create)
            waited = 0.0
            while waited <= FORK_POLL_MAX_SECONDS:
                if await self.reader.repo(full) is not None:
                    return full
                await self.sleep(FORK_POLL_SECONDS)
                waited += FORK_POLL_SECONDS
            raise SubmitFailed("fork_timeout")
        raise SubmitFailed("fork_name_taken")

    async def _push(self, snap: Snapshot, progress: Progress, fork: str) -> None:
        assert snap.head
        assert snap.branch
        assert snap.rev
        head, branch = snap.head, snap.branch
        mirror = self.ctx.gitops.mirror_dir(snap.upstream, snap.cid, snap.rev)
        would = {"fork": fork, "sha": head, "ref": f"refs/heads/{branch}"}
        if not snap.dry_run:
            tip = await self.reader.branch_tip(fork, branch)
            if tip == head:  # an earlier attempt already pushed exactly this SHA
                self._checkpoint("push", snap)
                return

        async def push() -> None:
            await asyncio.to_thread(self.writer.push, mirror, fork, head, branch)

        await self._write("push", snap, progress, would, push)

    async def _open_pr(self, snap: Snapshot, progress: Progress, login: str) -> None:
        fork = await self._fork(snap, progress, login)
        progress.result["fork"] = fork
        await self._push(snap, progress, fork)
        payload = {
            "title": snap.title, "body": snap.body, "head": f"{login}:{snap.branch}", "base": snap.base_ref,
            "maintainer_can_modify": True, "draft": False,
        }  # fmt: skip

        async def create() -> httpx.Response:
            return await self.writer.create_pull(snap.upstream, payload)

        resp = await self._write("pull_request", snap, progress, payload, create)
        if resp is None:
            return
        if resp.status_code == 422:
            existing = await self.reader.pulls(snap.upstream, state="all", head=f"{login}:{snap.branch}")
            if not existing:
                raise SubmitFailed("pull_request_failed", "422 without an existing PR")
            doc: dict[str, Any] = existing[0]
            self.ctx.notify(
                f"🚨 PR for {snap.cid} already existed (#{doc.get('number')}); reconciled, nothing reopened."
            )
        else:
            doc = resp.json()
        progress.result["pr_number"] = int(doc["number"])
        progress.result["pr_url"] = str(doc.get("html_url", ""))

    async def _push_update(self, snap: Snapshot, progress: Progress) -> None:
        assert snap.fork_repo
        assert snap.branch
        if not snap.dry_run:
            tip = await self.reader.branch_tip(snap.fork_repo, snap.branch)
            if tip != snap.old_head_sha:
                raise SubmitFailed("branch_moved")
        progress.result["fork"] = snap.fork_repo
        await self._push(snap, progress, snap.fork_repo)

    # ---------------------------------------------------------------- outcomes
    def _succeeded(self, snap: Snapshot, progress: Progress) -> str:
        ctx = self.ctx
        status = "dry_run_done" if snap.dry_run else "done"
        result = {"writes": progress.writes, **progress.result}
        if snap.dry_run:
            result["would"] = progress.would
        with db.transaction(ctx.conn):
            ctx.conn.execute(
                "UPDATE outbound_actions SET status = ?, result_json = ? WHERE id = ?",
                (status, json.dumps(result, sort_keys=True), snap.action_id),
            )
            if snap.dry_run:
                ctx.audit.append(
                    "gatekeeper_internal", "dry_run_would",
                    {"action_id": snap.action_id, "kind": snap.kind, "would": progress.would}, snap.cid,
                )  # fmt: skip
            if snap.kind == "open_pr" and snap.dry_run:
                ctx.state_machine.transition(snap.cid, State.SUBMITTING, State.DRY_RUN_DONE, _internal(), {})
            elif snap.kind == "push_update" and snap.dry_run:
                ctx.state_machine.transition(snap.cid, State.SUBMITTING, State.WATCHING, _internal(), {})
            elif snap.kind in PR_KINDS:
                now = to_iso(ctx.clock.now())
                if snap.kind == "open_pr":
                    ctx.conn.execute(
                        "UPDATE contributions SET pr_number = ?, pr_url = ?, head_sha = ?, branch = ?, updated_at = ?"
                        " WHERE id = ?",
                        (
                            progress.result["pr_number"],
                            progress.result["pr_url"],
                            snap.head,
                            snap.branch,
                            now,
                            snap.cid,
                        ),
                    )
                else:
                    ctx.conn.execute(
                        "UPDATE contributions SET head_sha = ?, updated_at = ? WHERE id = ?", (snap.head, now, snap.cid)
                    )
                ctx.state_machine.transition(snap.cid, State.SUBMITTING, State.SUBMITTED, _internal(), {})
                ctx.state_machine.transition(snap.cid, State.SUBMITTED, State.WATCHING, _internal(), {})
            ctx.audit.append(
                "gatekeeper_internal", "submit_done", {"action_id": snap.action_id, "status": status}, snap.cid
            )
        if snap.dry_run:
            what = "/".join(progress.would) or "nothing"
            ctx.notify(f"DRY RUN — nothing was published ({snap.cid}): would {what}.")
        elif snap.kind == "open_pr":
            ctx.notify(f"Submitted ✅ PR #{progress.result['pr_number']} {progress.result['pr_url']}")
        elif snap.kind == "push_update":
            ctx.notify(f"Pushed ✅ 1 commit to PR #{snap.pr_number} ({snap.upstream})")
        else:
            ctx.notify(f"Posted ✅ {progress.result.get('comment_url') or ''}")
        return status

    def _finish_failed(self, snap: Snapshot, error_class: str, result: dict[str, Any]) -> None:
        ctx = self.ctx
        with db.transaction(ctx.conn):
            ctx.conn.execute(
                "UPDATE outbound_actions SET status = 'failed', error_class = ?, result_json = ? WHERE id = ?",
                (error_class, json.dumps(result, sort_keys=True), snap.action_id),
            )
            if snap.kind in PR_KINDS:
                ctx.state_machine.transition(
                    snap.cid, State.SUBMITTING, State.SUBMIT_FAILED, _internal(), {"error_class": error_class}
                )
                if error_class == "branch_moved":
                    ctx.conn.execute(
                        "UPDATE contributions SET watch_rounds = ? WHERE id = ?",
                        (ctx.config.policy.limits.watch_rounds_max, snap.cid),
                    )
            ctx.audit.append(
                "gatekeeper_internal", "submit_failed",
                {"action_id": snap.action_id, "error_class": error_class, **result}, snap.cid,
            )  # fmt: skip

    def _interrupted(self, snap: Snapshot, progress: Progress, cause: Cause) -> str:
        self._finish_failed(snap, "interrupted", {"writes": progress.writes, "cause": cause, **progress.result})
        done = ", ".join(progress.writes) or "no writes"
        if cause == "mode_changed":
            now = "DRY RUN" if db.current_dry_run(self.ctx.conn) else "LIVE"
            text = (
                f"Submission interrupted: mode changed during submission (now {now}) after {done};"
                " nothing further was written. [Retry]"
            )
        elif cause == "held":
            text = HELD_TEXT.format(done=done)
        else:
            text = f"Submission interrupted by {cause} after {done}; nothing further was written. [Retry]"
        self.retry_note(snap.action_id, text)
        return "failed"

    def _failed(self, snap: Snapshot, progress: Progress, error_class: str, detail: str) -> str:
        self._finish_failed(snap, error_class, {"writes": progress.writes, "detail": detail[:500], **progress.result})
        self.retry_note(snap.action_id, f"❌ Submission failed ({error_class}) for {snap.cid}. [Retry]")
        return "failed"


def make_submitter(ctx: AppContext, client: httpx.AsyncClient, token: str | None) -> Submitter:
    """Production wiring: pushes go through ``GitPusher`` (askpass memfd) from the per-rev mirrors."""
    pusher = GitPusher(ctx.gitops.git, token or "", ctx.config.profile.github_login)
    return Submitter(ctx, client, pusher, token=token)


def live_input(snap: Snapshot, login: str) -> live_checks.LiveInput:
    return live_checks.LiveInput(
        kind=snap.kind, upstream=snap.upstream, login=login, issue_number=snap.issue_number,
        pr_number=snap.pr_number, branch=snap.branch, target_number=snap.target_number,
        old_head_sha=snap.old_head_sha, fork_repo=snap.fork_repo, since=snap.since, action_id=snap.action_id,
    )  # fmt: skip


def _human() -> Actor:
    return Actor.HUMAN


def _internal() -> Actor:
    return Actor.GATEKEEPER
