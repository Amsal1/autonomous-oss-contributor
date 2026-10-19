"""One open PR per repo, overridden per 👤 item (DESIGN §14.11.19 pre-checks, CR4-2c): Start work on a
user-directed proposal while our PR is open in the repo audits ``open_pr_override``, and the submit-time
live check then lifts ``open_prs_per_repo`` for that contribution only; ``prs_per_24h`` still applies."""

from __future__ import annotations

from ossbot.common.clock import to_iso
from ossbot.common.schemas import State
from ossbot.gatekeeper import db, live_checks
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.github_read import GitHubReader
from tests.conftest import insert_action, insert_contribution
from tests.unit.gatekeeper.test_live_checks import UP, inp, world
from tests.unit.gatekeeper.test_proposals import scouting, start, submit


def _open_pr_elsewhere(ctx: AppContext) -> None:
    insert_contribution(ctx, State.WATCHING, upstream=UP, pr_number=5678)


async def _checks(ctx: AppContext, cid: str, *, used_24h: int = 0) -> list[str]:
    gh = world("open_pr")
    gh.add_pull(UP, user={"login": "test-bot-login"}, head={"label": "test-bot-login:other"}, body="x")
    action = insert_action(ctx, cid, "open_pr", {})
    for _ in range(used_24h):
        insert_action(ctx, cid, "open_pr", {}, status="done", executed_at=to_iso(ctx.clock.now()))
    mine = live_checks.LiveInput(**{**inp("open_pr").__dict__, "action_id": action})
    return await live_checks.run(mine, GitHubReader(gh.client()), conn=ctx.conn, clock=ctx.clock,
                                 limits=ctx.config.policy.limits)  # fmt: skip


def overrides(ctx: AppContext, cid: str) -> int:
    return int(db.one(ctx.conn, "SELECT COUNT(*) FROM audit_events WHERE event = 'open_pr_override' AND cid = ?",
                      (cid,))[0])  # type: ignore[index]  # fmt: skip


async def test_user_item_confirmed_over_an_open_pr_is_allowed(ctx: AppContext) -> None:
    _open_pr_elsewhere(ctx)
    cid = scouting(ctx, source="user")
    start(ctx, submit(ctx, cid)["approval_id"])
    assert overrides(ctx, cid) == 1
    assert "rate_limited:open_prs_per_repo" not in await _checks(ctx, cid)


async def test_the_override_never_lifts_prs_per_24h(ctx: AppContext) -> None:
    _open_pr_elsewhere(ctx)
    cid = scouting(ctx, source="user")
    start(ctx, submit(ctx, cid)["approval_id"])
    assert "rate_limited:prs_per_24h" in await _checks(ctx, cid, used_24h=ctx.config.policy.limits.prs_per_24h)


async def test_autonomous_items_and_unconfirmed_ones_keep_the_limit(ctx: AppContext) -> None:
    _open_pr_elsewhere(ctx)
    auto = insert_contribution(ctx, State.PROPOSAL_APPROVED, upstream=UP)
    assert "rate_limited:open_prs_per_repo" in await _checks(ctx, auto)
    user = scouting(ctx, source="user")
    submit(ctx, user)  # proposed, never started: no override
    assert overrides(ctx, user) == 0
    assert "rate_limited:open_prs_per_repo" in await _checks(ctx, user)


async def test_no_open_pr_no_override(ctx: AppContext) -> None:
    cid = scouting(ctx, source="user")
    start(ctx, submit(ctx, cid)["approval_id"])
    assert overrides(ctx, cid) == 0
