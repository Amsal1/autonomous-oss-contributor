"""Re-discovery enforced by the gatekeeper on contribution insert (DESIGN §9.4, M6, AC 30)."""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import timedelta

import pytest

from ossbot.common.clock import FakeClock, to_iso
from ossbot.common.config import AiPolicyRules, VetPolicy
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.contributions import ContributionRefused
from tests.conftest import insert_contribution

REPO = "langfuse/langfuse"
ISSUE = 4242


def prior(ctx: AppContext, state: State, days_ago: float = 1, reason: str | None = None) -> str:
    ts = to_iso(ctx.clock.now() - timedelta(days=days_ago))
    return insert_contribution(ctx, state, upstream=REPO, issue_number=ISSUE, state_reason=reason,
                               created_at=ts, updated_at=ts)  # fmt: skip


def create(ctx: AppContext) -> str:
    return ctx.contributions.create(REPO, ISSUE, "discover", 1.0, "uid:2001/worker")


def refused(ctx: AppContext) -> str:
    with pytest.raises(ContributionRefused) as info:
        create(ctx)
    assert info.value.status == 409
    return info.value.code


def test_dry_run_done_with_dry_run_off_allows(ctx: AppContext) -> None:
    prior(ctx, State.DRY_RUN_DONE)
    ctx.control.set_dry_run(False, "tg:4242")
    assert create(ctx).startswith("c_")


def test_dry_run_done_with_dry_run_on_refused(ctx: AppContext) -> None:
    prior(ctx, State.DRY_RUN_DONE)
    assert refused(ctx) == "not_rediscoverable:dry_run_rehearsal"


@pytest.mark.parametrize(("days", "allowed"), [(6, False), (8, True)])
def test_failed_cooldown(ctx: AppContext, days: int, allowed: bool) -> None:
    prior(ctx, State.FAILED, days)
    if allowed:
        create(ctx)
    else:
        assert refused(ctx) == "not_rediscoverable:cooldown"


@pytest.mark.parametrize("state", [State.REJECTED, State.MERGED, State.CLOSED])
def test_never_states(ctx: AppContext, state: State) -> None:
    prior(ctx, state, 365)
    ctx.control.set_dry_run(False, "tg:4242")
    assert refused(ctx) == f"not_rediscoverable:never:{state.value}"


def test_skipped_claimed_by_other_never(ctx: AppContext) -> None:
    prior(ctx, State.SKIPPED, 365, "claimed_by_other")
    assert refused(ctx) == "not_rediscoverable:never:claimed_by_other"


def _with_ai_rules(ctx: AppContext, monkeypatch: pytest.MonkeyPatch, **rules: str) -> None:
    bundle = ctx.contributions.config()
    vet = VetPolicy(ai_policy=AiPolicyRules.model_validate(rules))
    patched = dataclasses.replace(bundle, policy=bundle.policy.model_copy(update={"vet": vet}))
    monkeypatch.setattr(ctx.contributions, "config", lambda: patched)


def test_ai_policy_unclear_until_allow_after_skip(ctx: AppContext, clock: FakeClock,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    """With the owner's rule ``unclear: skip`` the pre-post-v1 behaviour holds: only "Allow AI" lifts it."""
    _with_ai_rules(ctx, monkeypatch, unclear="skip")
    prior(ctx, State.SKIPPED, 3, "ai_policy_unclear")
    assert refused(ctx) == "not_rediscoverable:repo_fact_unchanged:ai_policy_unclear"
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO repo_overrides (upstream, ai_policy_override, updated_by, updated_at)"
            " VALUES (?, 'allowed', ?, ?)",
            (REPO, "tg:4242", to_iso(clock.now())),
        )
    assert create(ctx).startswith("c_")


def test_ai_policy_unclear_rediscoverable_under_the_default_rule(ctx: AppContext) -> None:
    """Post-v1 (§9.5): the default ``unclear: allow`` lifts an earlier ``ai_policy_unclear`` skip."""
    prior(ctx, State.SKIPPED, 1, "ai_policy_unclear")
    assert ctx.contributions.decide(REPO, ISSUE).rule == "ai_policy_rule_allow"
    assert create(ctx).startswith("c_")


def test_ai_policy_unstated_follows_its_own_rule(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    prior(ctx, State.SKIPPED, 1, "ai_policy_unstated")
    _with_ai_rules(ctx, monkeypatch, unstated="skip")
    assert refused(ctx) == "not_rediscoverable:repo_fact_unchanged:ai_policy_unstated"
    _with_ai_rules(ctx, monkeypatch, unstated="allow", unclear="skip")
    assert create(ctx).startswith("c_")


@pytest.mark.parametrize("reason", ["ai_policy_banned", "claimed_by_other"])
def test_ban_and_claim_stay_never_under_the_default_rule(ctx: AppContext, reason: str) -> None:
    prior(ctx, State.SKIPPED, 400, reason)
    assert refused(ctx) == f"not_rediscoverable:never:{reason}"


def test_second_active_contribution_is_duplicate(ctx: AppContext) -> None:
    prior(ctx, State.FETCHING)
    assert refused(ctx) == "duplicate_active"


def test_index_backstop_with_raw_insert(ctx: AppContext) -> None:
    prior(ctx, State.WATCHING)
    with pytest.raises(sqlite3.IntegrityError):
        insert_contribution(ctx, State.DISCOVERED, upstream=REPO, issue_number=ISSUE)
    # terminal rows never block
    insert_contribution(ctx, State.FAILED, upstream=REPO, issue_number=ISSUE)


def test_terminal_rows_do_not_block_index(ctx: AppContext) -> None:
    for state in (State.FAILED, State.ABANDONED, State.SKIPPED):
        prior(ctx, state, 30)
    assert create(ctx).startswith("c_")


def test_non_target_and_discovery_disabled_refused(ctx: AppContext) -> None:
    with pytest.raises(ContributionRefused) as info:
        ctx.contributions.create("nobody/unknown", 1, "discover", None, "w")
    assert info.value.code == "not_a_target"
    with pytest.raises(ContributionRefused) as info:
        ctx.contributions.create("tobenwarrior/hermes-kiro-provider", 1, "discover", None, "w")
    assert info.value.code == "discovery_disabled"


def test_created_row_and_audit(ctx: AppContext) -> None:
    cid = create(ctx)
    row = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
    assert row is not None and row["state"] == "DISCOVERED" and row["category"] == "llm-observability"
    assert ctx.conn.execute("SELECT COUNT(*) FROM audit_events WHERE event = 'contribution_created'").fetchone()[0] == 1
