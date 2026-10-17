"""Restart reconciliation of ``executing`` outbound actions by GitHub reads only (DESIGN §12.5, M5, I4).

Runs at gatekeeper start before Telegram polling and before any action can execute, even while
killed or paused (it writes nothing to GitHub). A failed read leaves the row ``executing`` with
``state_reason=reconcile_pending`` (retried every 5 minutes; rate limits keep counting it). Writes
are never resumed: the only way to another write is a fresh two-tap approval (Retry).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ossbot.common.clock import from_iso, to_iso
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.github_read import GitHubError, GitHubReader
from ossbot.gatekeeper.state_machine import Actor

log = logging.getLogger(__name__)
RECONCILE_INTERVAL_SECONDS = 300.0
RetryNote = Callable[[str, str], None]


@dataclass(frozen=True)
class Outcome:
    action_id: str
    result: str  # done | interrupted | branch_moved | pending


class Reconciler:
    def __init__(self, ctx: AppContext, reader: GitHubReader, retry_note: RetryNote | None = None) -> None:
        self.ctx = ctx
        self.reader = reader
        self.retry_note: RetryNote = retry_note or (lambda action_id, text: ctx.notify(text))

    def _dry_run_of(self, action_id: str) -> bool:
        row = db.one(
            self.ctx.conn,
            "SELECT binding_json FROM approvals WHERE action_id = ? AND status = 'consumed'"
            " ORDER BY consumed_at DESC LIMIT 1",
            (action_id,),
        )
        return True if row is None else bool(json.loads(row["binding_json"]).get("dry_run", True))

    async def run_once(self, only: set[str] | None = None) -> list[Outcome]:
        """At start: every ``executing`` row. Later (``only``): just the rows still pending from
        an earlier run, never a submission that is in flight in this process."""
        rows = self.ctx.conn.execute(
            "SELECT o.*, c.upstream, c.state AS c_state FROM outbound_actions o"
            " JOIN contributions c ON c.id = o.cid WHERE o.status = 'executing' ORDER BY o.executed_at"
        ).fetchall()
        if only is not None:
            rows = [r for r in rows if r["id"] in only]
        outcomes: list[Outcome] = []
        for row in rows:
            try:
                outcomes.append(await self._one(row))
            except GitHubError as exc:
                log.warning("reconcile read failed for %s: %s", row["id"], exc, extra={"event": "reconcile_pending"})
                with db.transaction(self.ctx.conn):
                    self.ctx.conn.execute(
                        "UPDATE contributions SET state_reason = 'reconcile_pending' WHERE id = ?", (row["cid"],)
                    )
                outcomes.append(Outcome(row["id"], "pending"))
        return outcomes

    async def _one(self, row: Any) -> Outcome:
        payload: dict[str, Any] = json.loads(row["payload_json"])
        login = self.ctx.config.profile.github_login
        kind = row["kind"]
        if self._dry_run_of(row["id"]):
            return self._not_found(row, "interrupted", "dry-run submission interrupted; nothing was written")
        upstream = row["upstream"]
        if kind == "open_pr":
            pulls = await self.reader.pulls(upstream, state="all", head=f"{login}:{payload['branch']}")
            if pulls:
                pr = pulls[0]
                return self._pr_found(row, payload, int(pr["number"]), str(pr.get("html_url", "")))
            return self._not_found(row, "interrupted", "no PR had been opened")
        if kind == "push_update":
            fork = payload.get("fork_repo") or f"{payload['fork_owner']}/{upstream.split('/', 1)[1]}"
            tip = await self.reader.branch_tip(fork, payload["branch"])
            if tip == payload["head_sha"]:
                return self._push_found(row, payload)
            if tip == payload["old_head_sha"]:
                return self._not_found(row, "interrupted", "the push had not happened")
            return self._not_found(row, "branch_moved", "the fork branch moved")
        since = from_iso(row["executed_at"]).strftime("%Y-%m-%dT%H:%M:%SZ")
        marker = f"<!-- ossbot:action={row['id']} -->"
        for comment in await self.reader.comments_since(upstream, int(payload["target_number"]), since):
            author = str((comment.get("user") or {}).get("login", "")).lower()
            if author == login.lower() and marker in str(comment.get("body") or ""):
                return self._comment_found(row, str(comment.get("html_url", "")))
        return self._not_found(row, "interrupted", "the comment had not been posted")

    # ----------------------------------------------------------------- outcomes
    def _done(self, row: Any, result: dict[str, Any]) -> None:
        self.ctx.conn.execute(
            "UPDATE outbound_actions SET status = 'done', result_json = ? WHERE id = ?",
            (json.dumps({"reconciled": True, **result}, sort_keys=True), row["id"]),
        )
        self.ctx.audit.append("system", "reconciled", {"action_id": row["id"], "result": "done", **result}, row["cid"])

    def _pr_found(self, row: Any, payload: dict[str, Any], number: int, url: str) -> Outcome:
        ctx = self.ctx
        with db.transaction(ctx.conn):
            self._done(row, {"pr_number": number, "pr_url": url})
            ctx.conn.execute(
                "UPDATE contributions SET pr_number = ?, pr_url = ?, head_sha = ?, branch = ?, state_reason = NULL,"
                " updated_at = ? WHERE id = ?",
                (number, url, payload["head_sha"], payload["branch"], to_iso(ctx.clock.now()), row["cid"]),
            )
            if row["c_state"] == State.SUBMITTING.value:
                ctx.state_machine.transition(row["cid"], State.SUBMITTING, State.SUBMITTED, Actor.GATEKEEPER, {})
                ctx.state_machine.transition(row["cid"], State.SUBMITTED, State.WATCHING, Actor.GATEKEEPER, {})
        ctx.notify(f"Recovered after restart: PR #{number} had been opened {url}")
        return Outcome(row["id"], "done")

    def _push_found(self, row: Any, payload: dict[str, Any]) -> Outcome:
        ctx = self.ctx
        with db.transaction(ctx.conn):
            self._done(row, {"pushed": payload["head_sha"]})
            ctx.conn.execute(
                "UPDATE contributions SET head_sha = ?, state_reason = NULL, updated_at = ? WHERE id = ?",
                (payload["head_sha"], to_iso(ctx.clock.now()), row["cid"]),
            )
            if row["c_state"] == State.SUBMITTING.value:
                ctx.state_machine.transition(row["cid"], State.SUBMITTING, State.SUBMITTED, Actor.GATEKEEPER, {})
                ctx.state_machine.transition(row["cid"], State.SUBMITTED, State.WATCHING, Actor.GATEKEEPER, {})
        ctx.notify(f"Recovered after restart: the fix for PR #{payload.get('pr_number')} had been pushed")
        return Outcome(row["id"], "done")

    def _comment_found(self, row: Any, url: str) -> Outcome:
        with db.transaction(self.ctx.conn):
            self._done(row, {"comment_url": url})
        self.ctx.notify(f"Recovered after restart: the comment had been posted {url}")
        return Outcome(row["id"], "done")

    def _not_found(self, row: Any, error_class: str, why: str) -> Outcome:
        ctx = self.ctx
        with db.transaction(ctx.conn):
            ctx.conn.execute(
                "UPDATE outbound_actions SET status = 'failed', error_class = ?, result_json = ? WHERE id = ?",
                (error_class, json.dumps({"reconciled": True, "why": why}), row["id"]),
            )
            if row["kind"] in {"open_pr", "push_update"} and row["c_state"] == State.SUBMITTING.value:
                ctx.state_machine.transition(
                    row["cid"], State.SUBMITTING, State.SUBMIT_FAILED, Actor.GATEKEEPER, {"error_class": error_class}
                )
                if error_class == "branch_moved":
                    ctx.conn.execute(
                        "UPDATE contributions SET watch_rounds = ? WHERE id = ?",
                        (ctx.config.policy.limits.watch_rounds_max, row["cid"]),
                    )
            ctx.audit.append(
                "system", "reconciled", {"action_id": row["id"], "result": error_class, "why": why}, row["cid"]
            )
        self.retry_note(
            row["id"], f"Recovered after restart: {why} ({row['cid']}); nothing further was written. [Retry]"
        )
        return Outcome(row["id"], error_class)


class ReconcileService:
    """Startup run before anything else; pending rows are retried every 5 minutes."""

    def __init__(self, reconciler: Reconciler, interval: float = RECONCILE_INTERVAL_SECONDS) -> None:
        self.reconciler = reconciler
        self.interval = interval
        self.pending: set[str] = set()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        outcomes = await self.reconciler.run_once()
        self.pending = {o.action_id for o in outcomes if o.result == "pending"}
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            if not self.pending:
                continue
            try:
                outcomes = await self.reconciler.run_once(self.pending)
            except Exception:
                log.exception("reconcile retry failed", extra={"event": "reconcile_failed"})
                continue
            self.pending = {o.action_id for o in outcomes if o.result == "pending"}

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
