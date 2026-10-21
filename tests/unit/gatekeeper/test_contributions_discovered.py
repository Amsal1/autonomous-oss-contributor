"""Discovered repos through contribution creation, ``ossctl discover`` and the CLA fact (design §6.4, §8.5;
review-3 M1)."""

from __future__ import annotations

from typing import Any

import pytest

from ossbot.common.clock import FakeClock
from ossbot.common.config import KEPT_PRIORITY, OPEN_CATEGORY, OPEN_PRIORITY
from ossbot.common.schemas import DiscoveredFacts
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.contributions import ContributionRefused, _cla_signed_at
from tests.conftest import UID_WORKER
from tests.unit.gatekeeper.api_helpers import client

WORKER = "uid:2001/worker"
OPERATOR = "uid:1000/operator"
REPO = "openfaas/faas"  # not in config/targets.yaml


def record(ctx: AppContext, repo: str = REPO, **kw: Any) -> None:
    facts = {"repo": repo, "source": "search", "status": "candidate", "stars": 25000, **kw}
    ctx.discovered.record(DiscoveredFacts.model_validate(facts), WORKER)


def create(ctx: AppContext, repo: str = REPO, issue: int = 7) -> str:
    return ctx.contributions.create(repo, issue, "discover", 1.0, WORKER)


def refused(ctx: AppContext, repo: str = REPO, issue: int = 7) -> ContributionRefused:
    with pytest.raises(ContributionRefused) as info:
        create(ctx, repo, issue)
    return info.value


def row(ctx: AppContext, cid: str) -> dict[str, Any]:
    found = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
    assert found is not None
    return dict(found)


def test_create_for_candidate_and_kept(ctx: AppContext) -> None:
    record(ctx)
    c = row(ctx, create(ctx))
    assert (c["upstream"], c["category"], c["priority"], c["state"]) == (REPO, OPEN_CATEGORY, OPEN_PRIORITY,
                                                                          "DISCOVERED")  # fmt: skip
    assert c["issue_url"] == f"https://github.com/{REPO}/issues/7"
    ctx.discovered.decide("fluxcd/flux2", "keep", OPERATOR)
    k = row(ctx, ctx.contributions.create("FluxCD/Flux2", 3, "hermes_request", None, WORKER))
    assert (k["upstream"], k["priority"], k["source"]) == ("fluxcd/flux2", KEPT_PRIORITY, "hermes_request")


def test_create_refusals(ctx: AppContext) -> None:
    assert (refused(ctx).status, refused(ctx).code) == (400, "not_a_target")  # absent
    ctx.discovered.suggest(REPO, None, "uid:2003/requester")
    assert refused(ctx).code == "not_a_target"  # suggested
    record(ctx, "o/skipped", status="skipped", skip_reason="no_contributing")
    assert refused(ctx, "o/skipped").code == "not_a_target"
    record(ctx)
    ctx.discovered.decide(REPO, "never", OPERATOR)
    err = refused(ctx)
    assert (err.status, err.code) == (400, "never_repo")
    assert refused(ctx, "LineageOS/android").code == "not_a_target"


def test_config_targets_unchanged(ctx: AppContext) -> None:
    c = row(ctx, create(ctx, "langfuse/langfuse", 11))
    assert c["category"] != OPEN_CATEGORY and c["priority"] == 1
    assert refused(ctx, "tobenwarrior/hermes-kiro-provider").code == "discovery_disabled"


def test_request_discover_accepts_config_targets_and_kept_repos(ctx: AppContext) -> None:
    out = ctx.contributions.request_discover(["langfuse/langfuse"], OPERATOR)
    assert out["repos"] == ["langfuse/langfuse"]
    with pytest.raises(ContributionRefused, match="not_a_target"):
        ctx.contributions.request_discover([REPO], OPERATOR)
    record(ctx)
    with pytest.raises(ContributionRefused, match="not_a_target"):  # a candidate is not a kept target
        ctx.contributions.request_discover([REPO], OPERATOR)
    ctx.discovered.decide(REPO, "keep", OPERATOR)
    assert ctx.contributions.request_discover([f"https://github.com/{REPO}"], OPERATOR)["repos"] == [REPO]


def test_cla_signed_at_falls_back_to_defaults(ctx: AppContext) -> None:
    """Shipped targets.yaml has defaults.cla_signed true: a non-config repo inherits it."""
    assert ctx.config.targets.defaults.cla_signed is True
    assert _cla_signed_at(ctx.conn, REPO) is not None
    assert _cla_signed_at(ctx.conn, REPO) == _cla_signed_at(ctx.conn, "langfuse/langfuse")


async def test_m1_in_flight_contribution_keeps_its_target(ctx: AppContext, clock: FakeClock) -> None:
    """Review-3 M1: the row moves candidate -> skipped while a contribution is in flight; resolution still
    works for it, but no new contribution can be created for the repo."""
    record(ctx, tried=True)
    cid = create(ctx)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE contributions SET state = 'FETCHING' WHERE id = ?", (cid,))
    clock.advance(days=15)
    record(ctx, status="skipped", skip_reason="no_contributing")
    assert ctx.discovered.get(REPO).status == "skipped"  # type: ignore[union-attr]
    target = ctx.discovered.effective_target(REPO)
    assert target is not None and target.category == OPEN_CATEGORY
    async with client(ctx, UID_WORKER) as c:
        doc = (await c.get("/v1/targets/resolve", params={"repo": REPO})).json()
        assert doc["source"] == "discovered" and doc["target"]["repo"] == REPO
        resp = await c.post("/v1/contributions", json={"repo": REPO, "issue_number": 8, "source": "discover"})
        assert (resp.status_code, resp.json()["error"]) == (400, "not_a_target")
    assert refused(ctx, REPO, 9).code == "not_a_target"
