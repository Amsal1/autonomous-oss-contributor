"""Rate limits (DESIGN §12.6, §12.7, M10).

Counters derive from ``outbound_actions`` with ``status IN ('executing','done')`` and
``executed_at`` in the rolling window; ``executing`` counts as consumed so an unresolved row can
never free a slot. Dry-run actions (``dry_run_done``) never count. ``open_pr`` additionally uses
the live GitHub count of the login's open PRs on the repo.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

import httpx

from ossbot.common.clock import Clock, to_iso
from ossbot.common.config import Limits
from ossbot.common.schemas import OutboundKind

WINDOW = timedelta(hours=24)
CONSUMING_STATUSES = ("executing", "done")


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    counter: str | None
    used: int
    limit: int | None

    @property
    def code(self) -> str:
        return "ok" if self.allowed else f"rate_limited:{self.counter}"


def used_in_window(
    conn: sqlite3.Connection, clock: Clock, kind: OutboundKind, exclude_action_id: str | None = None
) -> int:
    """``exclude_action_id``: the action being submitted (it is ``executing`` itself at submit time)."""
    since = to_iso(clock.now() - WINDOW)
    row = conn.execute(
        "SELECT COUNT(*) FROM outbound_actions WHERE kind = ? AND status IN ('executing','done')"
        " AND executed_at IS NOT NULL AND executed_at > ? AND id IS NOT ?",
        (kind, since, exclude_action_id),
    ).fetchone()
    return int(row[0])


OpenPrCounter = Callable[[str], Awaitable[int]]


def github_open_pr_counter(client: httpx.AsyncClient, login: str) -> OpenPrCounter:
    """Live count of ``login``'s open PRs on ``repo`` via the search API (GitHub read token)."""

    async def count(repo: str) -> int:
        resp = await client.get(
            "/search/issues", params={"q": f"repo:{repo} is:pr is:open author:{login}", "per_page": "1"}
        )
        resp.raise_for_status()
        total = resp.json().get("total_count")
        if not isinstance(total, int) or total < 0:
            raise ValueError("search response without total_count")
        return total

    return count


async def check(
    conn: sqlite3.Connection,
    clock: Clock,
    limits: Limits,
    kind: OutboundKind,
    upstream: str,
    open_pr_counter: OpenPrCounter | None = None,
    *,
    exclude_action_id: str | None = None,
) -> RateDecision:
    if kind == "push_update":
        return RateDecision(True, None, 0, None)  # bounded by watch_rounds_max and owner_fix_rounds_max instead
    if kind == "comment":
        used = used_in_window(conn, clock, "comment", exclude_action_id)
        return RateDecision(used < limits.comments_per_24h, "comments_per_24h", used, limits.comments_per_24h)
    if kind == "issue_claim":
        used = used_in_window(conn, clock, "issue_claim", exclude_action_id)
        return RateDecision(
            used < limits.issue_claims_per_24h, "issue_claims_per_24h", used, limits.issue_claims_per_24h
        )
    used = used_in_window(conn, clock, "open_pr", exclude_action_id)
    if used >= limits.prs_per_24h:
        return RateDecision(False, "prs_per_24h", used, limits.prs_per_24h)
    if open_pr_counter is None:
        return RateDecision(False, "open_prs_per_repo", -1, limits.open_prs_per_repo)  # fail closed
    open_count = await open_pr_counter(upstream)
    # Our own executing open_pr rows on this repo count too (the PR may not exist on GitHub yet).
    executing = int(
        conn.execute(
            "SELECT COUNT(*) FROM outbound_actions o JOIN contributions c ON c.id = o.cid"
            " WHERE o.kind = 'open_pr' AND o.status = 'executing' AND c.upstream = ? AND o.id IS NOT ?",
            (upstream, exclude_action_id),
        ).fetchone()[0]
    )
    total = open_count + executing
    return RateDecision(total < limits.open_prs_per_repo, "open_prs_per_repo", total, limits.open_prs_per_repo)
