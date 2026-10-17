"""Live checks at Confirm and at submit, per action kind (DESIGN §12.6 table, M10). Reads only.

"Other open PR" means an open PR that references the issue and whose head is not
``<login>:<this contribution's branch>``; our own PR never counts as competing.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ossbot.common.clock import Clock, from_iso
from ossbot.common.config import Limits
from ossbot.common.schemas import OutboundKind
from ossbot.gatekeeper import ratelimit
from ossbot.gatekeeper.github_read import GitHubReader
from ossbot.gatekeeper.gitops import GitError, GitOps

# Same claim phrasing family as vetting (§9.5): somebody else said they are working on it.
CLAIM_RE = re.compile(
    r"\b(i'?d like to (work on|take)|i want to (work on|take)|can i (work on|take|pick)|"
    r"assign (this|it) to me|i'?ll (take|work on)|i will (take|work on)|i'?m working on|i am working on)\b",
    re.IGNORECASE,
)
MERGED_THANKS_WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class LiveInput:
    kind: OutboundKind
    upstream: str
    login: str
    issue_number: int | None
    pr_number: int | None
    branch: str | None
    target_number: int | None
    old_head_sha: str | None
    fork_repo: str | None
    since: str | None  # vetting time (claim checks)
    action_id: str | None = None


def references(item: dict[str, Any], number: int) -> bool:
    text = f"{item.get('title') or ''}\n{item.get('body') or ''}"
    return re.search(rf"(#|/issues/){number}\b", text) is not None


async def _repo_ok(reader: GitHubReader, upstream: str) -> list[str]:
    repo = await reader.repo(upstream)
    if repo is None:
        return ["repo_missing"]
    return ["repo_archived"] if repo.get("archived") else []


async def _competing_prs(reader: GitHubReader, inp: LiveInput, issue: int) -> list[dict[str, Any]]:
    own = f"{inp.login}:{inp.branch}" if inp.branch else None
    return [
        pr
        for pr in await reader.pulls(inp.upstream)
        if references(pr, issue) and (pr.get("head") or {}).get("label") != own
    ]


async def run(
    inp: LiveInput,
    reader: GitHubReader,
    *,
    conn: sqlite3.Connection,
    clock: Clock,
    limits: Limits,
) -> list[str]:
    """Return failed check codes (empty = all passed), including the per-kind rate limits."""
    failed = await _repo_ok(reader, inp.upstream)
    login = inp.login.lower()
    if inp.kind == "open_pr":
        if inp.issue_number is not None:
            issue = await reader.issue(inp.upstream, inp.issue_number)
            if issue is None or issue.get("state") != "open":
                failed.append("issue_not_open")
            elif any(str(a.get("login", "")).lower() != login for a in issue.get("assignees") or []):
                failed.append("issue_assigned_to_other")
            if await _competing_prs(reader, inp, inp.issue_number):
                failed.append("competing_pr")
        own = f"{inp.login}:{inp.branch}" if inp.branch else None
        open_prs = [
            pr
            for pr in await reader.pulls(inp.upstream)
            if str((pr.get("user") or {}).get("login", "")).lower() == login
            and (pr.get("head") or {}).get("label") != own
        ]

        async def counter(_repo: str) -> int:
            return len(open_prs)

        decision = await ratelimit.check(
            conn, clock, limits, "open_pr", inp.upstream, counter, exclude_action_id=inp.action_id
        )
        if not decision.allowed:
            failed.append(decision.code)
    elif inp.kind == "push_update":
        pr = await reader.pull(inp.upstream, inp.pr_number or 0)
        if pr is None or pr.get("state") != "open":
            failed.append("pr_not_open")
        else:
            if str((pr.get("user") or {}).get("login", "")).lower() != login:
                failed.append("pr_not_ours")
            if (pr.get("head") or {}).get("label") != f"{inp.login}:{inp.branch}":
                failed.append("pr_head_mismatch")
        tip = await reader.branch_tip(inp.fork_repo or "", inp.branch or "") if inp.fork_repo else None
        if tip is None or tip != inp.old_head_sha:
            failed.append("branch_moved")
    elif inp.kind == "comment":
        number = inp.target_number or 0
        if number not in {inp.issue_number, inp.pr_number} - {None}:
            failed.append("target_not_ours")
        issue = await reader.issue(inp.upstream, number)
        if issue is None:
            failed.append("target_missing")
        elif issue.get("state") != "open":
            merged_recently = False
            if issue.get("pull_request"):
                pr = await reader.pull(inp.upstream, number)
                merged_at = (pr or {}).get("merged_at")
                if isinstance(merged_at, str):
                    merged_recently = clock.now() - from_iso(merged_at) <= MERGED_THANKS_WINDOW
            if not merged_recently:
                failed.append("target_not_open")
        decision = await ratelimit.check(conn, clock, limits, "comment", inp.upstream, exclude_action_id=inp.action_id)
        if not decision.allowed:
            failed.append(decision.code)
    else:  # issue_claim
        number = inp.target_number or 0
        issue = await reader.issue(inp.upstream, number)
        if issue is None or issue.get("state") != "open":
            failed.append("issue_not_open")
        elif issue.get("assignees"):
            failed.append("issue_assigned")
        since = from_iso(inp.since).strftime("%Y-%m-%dT%H:%M:%SZ") if inp.since else "1970-01-01T00:00:00Z"
        for comment in await reader.comments_since(inp.upstream, number, since):
            author = str((comment.get("user") or {}).get("login", "")).lower()
            if author != login and CLAIM_RE.search(str(comment.get("body") or "")):
                failed.append("claimed_by_other")
                break
        if await _competing_prs(reader, inp, number):
            failed.append("competing_pr")
        decision = await ratelimit.check(
            conn, clock, limits, "issue_claim", inp.upstream, exclude_action_id=inp.action_id
        )
        if not decision.allowed:
            failed.append(decision.code)
    return failed


def upstream_ancestry(ops: GitOps, upstream: str, mirror: Any, base_ref: str, base_sha: str) -> list[str]:
    """Re-fetch ``refs/upstream-live/<base_ref>`` (via the cache) and require ``base_sha`` in it."""
    try:
        ops.fetch_upstream(upstream, base_ref, live=mirror)
    except (GitError, ValueError):
        return ["base_ref_unavailable"]
    if not ops.is_ancestor(mirror, base_sha, f"refs/upstream-live/{base_ref}"):
        return ["base_not_in_upstream"]
    return []
