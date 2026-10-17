"""Re-discovery rules table (DESIGN §9.4, M6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ossbot.common.rediscover import PriorContribution, RepoFactChanges, eligible
from ossbot.common.schemas import State

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)


def row(state: State, days_ago: float = 1, reason: str | None = None) -> PriorContribution:
    return PriorContribution("c_X", state, reason, NOW - timedelta(days=days_ago))


def decide(*rows: PriorContribution, dry_run: bool = True, facts: RepoFactChanges | None = None) -> tuple[bool, str]:
    d = eligible(list(rows), dry_run=dry_run, now=NOW, cooldown_days=7, facts=facts)
    return d.allowed, d.rule


@pytest.mark.parametrize(
    ("rows", "dry_run", "expected"),
    [
        ((), True, (True, "new")),
        ((row(State.DRY_RUN_DONE),), False, (True, "after_dry_run")),
        ((row(State.DRY_RUN_DONE),), True, (False, "dry_run_rehearsal")),
        ((row(State.FAILED, 6),), True, (False, "cooldown")),
        ((row(State.FAILED, 8),), True, (True, "cooldown_elapsed")),
        ((row(State.ABANDONED, 7),), True, (True, "cooldown_elapsed")),
        ((row(State.EXPIRED, 2),), True, (False, "cooldown")),
        ((row(State.SKIPPED, 2, "too_large"),), True, (False, "cooldown")),
        ((row(State.SKIPPED, 9, "too_large"),), True, (True, "cooldown_elapsed")),
        ((row(State.REJECTED, 400),), False, (False, "never:REJECTED")),
        ((row(State.MERGED, 400),), False, (False, "never:MERGED")),
        ((row(State.CLOSED, 400),), False, (False, "never:CLOSED")),
        ((row(State.SKIPPED, 400, "claimed_by_other"),), False, (False, "never:claimed_by_other")),
        ((row(State.SKIPPED, 400, "ai_policy_banned"),), False, (False, "never:ai_policy_banned")),
        ((row(State.SKIPPED, 400, "not_found"),), False, (False, "never:not_found")),
        ((row(State.WATCHING),), False, (False, "duplicate_active")),
        ((row(State.FAILED, 30), row(State.REJECTED, 60)), False, (False, "never:REJECTED")),  # any row counts
        ((row(State.DRY_RUN_DONE, 1), row(State.FAILED, 2)), False, (True, "after_dry_run")),  # newest decides
    ],
)
def test_rules(rows: tuple[PriorContribution, ...], dry_run: bool, expected: tuple[bool, str]) -> None:
    assert decide(*rows, dry_run=dry_run) == expected


def test_ai_policy_unclear_needs_later_allow() -> None:
    skipped = row(State.SKIPPED, 30, "ai_policy_unclear")
    assert decide(skipped) == (False, "repo_fact_unchanged:ai_policy_unclear")
    before = RepoFactChanges(ai_allowed_at=NOW - timedelta(days=31))
    assert decide(skipped, facts=before)[0] is False
    after = RepoFactChanges(ai_allowed_at=NOW - timedelta(days=1))
    assert decide(skipped, facts=after) == (True, "ai_allowed")


def test_cla_required_needs_newer_snapshot() -> None:
    skipped = row(State.SKIPPED, 10, "cla_required")
    assert decide(skipped)[0] is False
    assert decide(skipped, facts=RepoFactChanges(cla_signed_at=NOW))[0] is True
