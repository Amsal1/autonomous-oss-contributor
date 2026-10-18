"""Re-discovery rules (DESIGN §9.4, M6; CR-004 §9.2 for gate-1 declines).

Deterministic and evaluated over every contribution for one ``(upstream, issue_number)``. The
gatekeeper enforces them in ``POST /v1/contributions``; the runner calls the same function first
only to avoid useless calls.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from ossbot.common.schemas import TERMINAL_STATES, State

NEVER_STATES = frozenset({State.REJECTED, State.MERGED, State.CLOSED})
NEVER_RETRY_SKIP_REASONS = frozenset(
    {"claimed_by_other", "claim_rejected", "ai_policy_banned", "signed_commits_required", "not_found"}
)
COOLDOWN_STATES = frozenset({State.FAILED, State.ABANDONED, State.EXPIRED, State.SKIPPED})
# CR-004 gate 1: "Never this repo / kind" is never re-proposed. A snooze (Later) stays non-terminal
# (PROPOSAL_SNOOZED) and is re-scouted by the runner after it ends; PROPOSAL_EXPIRED likewise.
NEVER_PROPOSAL_REASONS = frozenset({"never_repo", "never_kind"})


@dataclass(frozen=True)
class PriorContribution:
    cid: str
    state: State
    state_reason: str | None
    updated_at: datetime
    issue_hash: str | None = None  # sha256 of title, body and labels when it was discovered


@dataclass(frozen=True)
class RepoFactChanges:
    """When the blocking repo facts last changed in favour of a retry (None = never)."""

    ai_allowed_at: datetime | None = None
    cla_signed_at: datetime | None = None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    rule: str

    @property
    def http_code(self) -> str:
        return "duplicate_active" if self.rule == "duplicate_active" else f"not_rediscoverable:{self.rule}"


def _skip_reason(row: PriorContribution) -> str:
    return (row.state_reason or "").split(":", 1)[0]


def eligible(
    history: Sequence[PriorContribution],
    *,
    dry_run: bool,
    now: datetime,
    cooldown_days: int,
    facts: RepoFactChanges | None = None,
    issue_hash: str | None = None,
) -> Decision:
    facts = facts or RepoFactChanges()
    if not history:
        return Decision(True, "new")
    rows = sorted(history, key=lambda r: r.updated_at, reverse=True)
    if any(r.state not in TERMINAL_STATES for r in rows):
        return Decision(False, "duplicate_active")
    for row in rows:
        if row.state in NEVER_STATES:
            return Decision(False, f"never:{row.state.value}")
        if row.state is State.SKIPPED and _skip_reason(row) in NEVER_RETRY_SKIP_REASONS:
            return Decision(False, f"never:{_skip_reason(row)}")
        if row.state is State.PROPOSAL_REJECTED and _skip_reason(row) in NEVER_PROPOSAL_REASONS:
            return Decision(False, f"never:{_skip_reason(row)}")
    newest = rows[0]
    if newest.state is State.PROPOSAL_REJECTED:  # CR-004 Skip: only on a material change to the issue
        if issue_hash is not None and newest.issue_hash is not None and issue_hash != newest.issue_hash:
            return Decision(True, "material_change")
        return Decision(False, "skipped_unchanged")
    if newest.state is State.DRY_RUN_DONE:
        return Decision(True, "after_dry_run") if not dry_run else Decision(False, "dry_run_rehearsal")
    if newest.state is State.SKIPPED and _skip_reason(newest) == "ai_policy_unclear":
        if facts.ai_allowed_at is not None and facts.ai_allowed_at > newest.updated_at:
            return Decision(True, "ai_allowed")
        return Decision(False, "repo_fact_unchanged:ai_policy_unclear")
    if newest.state is State.SKIPPED and _skip_reason(newest) == "cla_required":
        if facts.cla_signed_at is not None and facts.cla_signed_at > newest.updated_at:
            return Decision(True, "cla_signed")
        return Decision(False, "repo_fact_unchanged:cla_required")
    if newest.state in COOLDOWN_STATES:
        if now - newest.updated_at >= timedelta(days=cooldown_days):
            return Decision(True, "cooldown_elapsed")
        return Decision(False, "cooldown")
    return Decision(False, f"unhandled:{newest.state.value}")  # pragma: no cover - all terminal states handled
