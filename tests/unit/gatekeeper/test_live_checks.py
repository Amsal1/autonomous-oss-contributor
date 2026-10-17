"""Per-kind live checks at Confirm/submit (DESIGN §12.6 table, M10), including own-PR exclusion."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from ossbot.common.clock import to_iso
from ossbot.common.schemas import OutboundKind
from ossbot.gatekeeper import live_checks
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.github_read import GitHubReader
from tests.conftest import insert_action, insert_contribution
from tests.fakes.fake_github import FakeGitHub

UP = "langfuse/langfuse"
FORK = "test-bot-login/langfuse"
OWN = "ossbot/12-fix-abc123"
Mutator = Callable[[FakeGitHub, AppContext], None]


def world(kind: OutboundKind) -> FakeGitHub:
    gh = FakeGitHub()
    gh.add_repo(UP)
    gh.add_issue(UP, 12)
    if kind in {"push_update", "comment"}:
        gh.add_pull(UP, number=41, user={"login": "test-bot-login"}, head={"label": f"test-bot-login:{OWN}"},
                    body="Fixes #12")  # fmt: skip
        gh.pulls[UP][-1]["number"] = 41
        gh.add_issue(UP, 41, pull_request={"url": "x"})
        gh.refs[(FORK, OWN)] = "9" * 40
    return gh


def inp(kind: OutboundKind) -> live_checks.LiveInput:
    return live_checks.LiveInput(
        kind=kind, upstream=UP, login="test-bot-login", issue_number=12,
        pr_number=41 if kind in {"push_update", "comment"} else None, branch=OWN,
        target_number={"comment": 41, "issue_claim": 12}.get(kind), old_head_sha="9" * 40,
        fork_repo=FORK, since="2026-10-16T00:00:00.000000Z",
    )  # fmt: skip


def consume(kind: OutboundKind, n: int) -> Mutator:
    def go(gh: FakeGitHub, ctx: AppContext) -> None:
        cid = insert_contribution(ctx)
        for _ in range(n):
            insert_action(ctx, cid, kind, {}, status="done", executed_at=to_iso(ctx.clock.now()))

    return go


def nothing(gh: FakeGitHub, ctx: AppContext) -> None:
    return None


def set_issue(**doc: Any) -> Mutator:
    return lambda gh, ctx: gh.issues[(UP, 12)].update(doc)


def other_pr(**doc: Any) -> Mutator:
    return lambda gh, ctx: (gh.add_pull(UP, user={"login": "someone"}, head={"label": "someone:fix"}, **doc), None)[1]


CASES: list[tuple[OutboundKind, str, Mutator, set[str]]] = [
    ("open_pr", "pass", nothing, set()),
    ("open_pr", "archived", lambda gh, ctx: gh.repos[UP].update(archived=True), {"repo_archived"}),
    ("open_pr", "issue closed", set_issue(state="closed"), {"issue_not_open"}),
    ("open_pr", "assigned to other", set_issue(assignees=[{"login": "someone"}]), {"issue_assigned_to_other"}),
    ("open_pr", "assigned to us", set_issue(assignees=[{"login": "test-bot-login"}]), set()),
    ("open_pr", "competing PR", other_pr(body="Closes #12"), {"competing_pr"}),
    ("open_pr", "unrelated PR", other_pr(body="Fixes #120"), set()),
    ("open_pr", "own PR not competing",
     lambda gh, ctx: (gh.add_pull(UP, user={"login": "test-bot-login"}, head={"label": f"test-bot-login:{OWN}"},
                                  body="Fixes #12"), None)[1], set()),
    ("open_pr", "another own open PR",
     lambda gh, ctx: (gh.add_pull(UP, user={"login": "test-bot-login"}, head={"label": "test-bot-login:other"},
                                  body="x"), None)[1], {"rate_limited:open_prs_per_repo"}),
    ("open_pr", "prs_per_24h used", consume("open_pr", 2), {"rate_limited:prs_per_24h"}),
    ("push_update", "pass", nothing, set()),
    ("push_update", "passes with zero prs_per_24h left", consume("open_pr", 2), set()),
    ("push_update", "pr closed", lambda gh, ctx: gh.pulls[UP][0].update(state="closed"), {"pr_not_open"}),
    ("push_update", "pr not ours", lambda gh, ctx: gh.pulls[UP][0].update(user={"login": "x"}), {"pr_not_ours"}),
    ("push_update", "head mismatch", lambda gh, ctx: gh.pulls[UP][0].update(head={"label": "test-bot-login:x"}),
     {"pr_head_mismatch"}),
    ("push_update", "fork moved", lambda gh, ctx: gh.refs.__setitem__((FORK, OWN), "8" * 40), {"branch_moved"}),
    ("comment", "pass on own PR at open_prs_per_repo limit", nothing, set()),
    ("comment", "target closed", lambda gh, ctx: gh.issues[(UP, 41)].update(state="closed"), {"target_not_open"}),
    ("comment", "merged recently (thank-you)",
     lambda gh, ctx: (gh.issues[(UP, 41)].update(state="closed"),
                      gh.pulls[UP][0].update(merged_at="2026-10-15T12:00:00Z"), None)[2], set()),
    ("comment", "merged long ago",
     lambda gh, ctx: (gh.issues[(UP, 41)].update(state="closed"),
                      gh.pulls[UP][0].update(merged_at="2026-10-01T12:00:00Z"), None)[2], {"target_not_open"}),
    ("comment", "comments_per_24h used", consume("comment", 10), {"rate_limited:comments_per_24h"}),
    ("issue_claim", "pass", nothing, set()),
    ("issue_claim", "assigned", set_issue(assignees=[{"login": "someone"}]), {"issue_assigned"}),
    ("issue_claim", "claimed by other",
     lambda gh, ctx: gh.comments.__setitem__((UP, 12), [{"user": {"login": "rival"}, "body": "Can I work on this?"}]),
     {"claimed_by_other"}),
    ("issue_claim", "own claim-like comment ignored",
     lambda gh, ctx: gh.comments.__setitem__((UP, 12), [{"user": {"login": "test-bot-login"},
                                                          "body": "I'd like to work on this"}]), set()),
    ("issue_claim", "competing PR", other_pr(body="Fixes #12"), {"competing_pr"}),
    ("issue_claim", "claims used", consume("issue_claim", 2), {"rate_limited:issue_claims_per_24h"}),
]  # fmt: skip


@pytest.mark.parametrize(("kind", "label", "mutate", "expected"), CASES, ids=[f"{c[0]}:{c[1]}" for c in CASES])
async def test_live_checks(
    ctx: AppContext, kind: OutboundKind, label: str, mutate: Mutator, expected: set[str]
) -> None:
    gh = world(kind)
    mutate(gh, ctx)
    failed = await live_checks.run(
        inp(kind), GitHubReader(gh.client()), conn=ctx.conn, clock=ctx.clock, limits=ctx.config.policy.limits
    )
    assert set(failed) == expected
    assert gh.writes == []


async def test_own_executing_action_not_counted(ctx: AppContext) -> None:
    """At submit time the action itself is executing; it must not count against its own limit."""
    gh = world("open_pr")
    cid = insert_contribution(ctx)
    insert_action(ctx, cid, "open_pr", {}, status="done", executed_at=to_iso(ctx.clock.now()))
    me = insert_action(ctx, cid, "open_pr", {}, status="executing", executed_at=to_iso(ctx.clock.now()))
    base = inp("open_pr")
    mine = live_checks.LiveInput(**{**base.__dict__, "action_id": me})
    reader = GitHubReader(gh.client())
    limits = ctx.config.policy.limits
    assert await live_checks.run(mine, reader, conn=ctx.conn, clock=ctx.clock, limits=limits) == []
    other = await live_checks.run(base, reader, conn=ctx.conn, clock=ctx.clock, limits=limits)
    assert "rate_limited:prs_per_24h" in other
