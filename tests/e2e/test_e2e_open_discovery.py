"""Post-v1 open discovery end to end (design §16 e2e scenarios 1-5 and 7): repos outside targets.yaml
found by topic search, vetted and scouted like seed targets, shown on a proposal card with the
open-discovery line and the Keep/Never row, Hermes suggestions, and a search rate limit that defers
the cycle. Every component is real (scheduler, stages, gatekeeper, sandboxd on the fake runtime, the
Telegram handlers over the fake Bot API); GitHub is ``tests/fakes/fake_github.py``.

Scenario 7 holds throughout: FakeGitHub records no write and the dry-run flag is never touched.
Scenario 6 (the egress anomaly hold) belongs to the egress work and is not here.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

import pytest

from ossbot.common.config import OPEN_CATEGORY
from ossbot.common.schemas import PrefsUpdate
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.runner import open_discovery as od
from ossbot.runner.github_read import GitHubRead
from ossbot.runner.stages import discover
from tests.conftest import UID_REQUESTER
from tests.e2e.conftest import E2E, build_e2e
from tests.githarness import GitWorld
from tests.telegram.proposalkit import card_of
from tests.telegram.reviewkit import labels, text, token
from tests.telegram.tgharness import TgRig, make_rig
from tests.unit.gatekeeper.api_helpers import client
from tests.unit.runner.test_discover_open import issue, issue_searches, open_repo, row

pytestmark = pytest.mark.timeout(300)

SEED = "langfuse/langfuse"
OPEN = "acme/gitops-engine"  # not in config/targets.yaml
KEEP_ROW = ["⭐ Keep as target", "🚫 Never this repo"]


class Rig:
    """The e2e harness and the Telegram rig on the same gatekeeper context."""

    def __init__(self, e2e: E2E, tg: TgRig) -> None:
        self.e2e, self.tg = e2e, tg

    @property
    def ctx(self) -> AppContext:
        return self.e2e.ctx

    def cycle(self) -> list[str]:
        """One full discovery cycle through the scheduler (the open phase included)."""
        return self.e2e.ready_scheduler().discover()

    async def card(self, cid: str) -> int:
        """Run the item to gate 1 and deliver its proposal card; returns the card's message id."""
        assert self.e2e.run_until(cid, "PROPOSED") == "PROPOSED", self.e2e.why(cid)
        await self.tg.flush()
        pid = db.one(self.ctx.conn, "SELECT id FROM proposal_approvals WHERE cid = ? AND status = 'pending'", (cid,))
        assert pid is not None, self.e2e.why(cid)
        return card_of(self.ctx, str(pid[0])).message_id

    async def tap(self, mid: int, label: str) -> None:
        await self.tg.tap(token(self.tg, mid, label), message=mid)

    def upstream(self, cid: str) -> str:
        return str(self.e2e.sc.gk.contribution(cid)["upstream"])

    def untouched(self) -> None:
        """Scenario 7: nothing was written to GitHub and dry-run is still on (its default)."""
        assert self.e2e.writes() == []
        assert db.current_dry_run(self.ctx.conn) is True


@pytest.fixture
async def rig(gctx: AppContext, world: GitWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    clock = gctx.clock

    class Clocked(datetime):
        """``discover.run`` reads the wall clock when the scheduler starts a cycle: the test clock."""

        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Clocked:  # type: ignore[override]
            return clock.now()  # type: ignore[return-value]

    monkeypatch.setattr(discover, "datetime", Clocked)
    with build_e2e(gctx, world, tmp_path) as e2e:
        wall = lambda: clock.now().timestamp()  # noqa: E731 - the search bucket follows the test clock
        e2e.sc.gh = GitHubRead(e2e.gh.sync_client(), sleep=lambda s: None, wall=wall, monotonic=wall)
        # the owner's /filters choice: search only this topic (a preference, not a count or budget)
        gctx.discovery_prefs.update(PrefsUpdate(filters={"open_topics": ["gitops"]}), "tg:4242")
        async for tg in make_rig(gctx):
            assert db.current_dry_run(gctx.conn) is True
            yield Rig(e2e, tg)


def no_seed_issue(r: Rig) -> None:
    r.e2e.gh.issues[(SEED, r.e2e.upstream.issue)]["state"] = "closed"


def go_repo(r: Rig, name: str = OPEN, number: int = 41) -> None:
    open_repo(r.e2e.gh, r.ctx, name, topic="gitops", stars=24440)
    issue(r.e2e.gh, r.ctx, name, number)


async def ask(ctx: AppContext, url: str) -> tuple[int, dict[str, Any]]:
    """Hermes's ``ossbot_request_discovery`` tool: ``POST /v1/discovery-requests`` as the requester."""
    async with client(ctx, UID_REQUESTER) as c:
        resp = await c.post("/v1/discovery-requests", json={"url": url})
    return resp.status_code, resp.json()


# ---------------------------------------------------------------- scenario 1
async def test_one_cycle_finds_the_go_repo_and_its_card(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="ossbot.runner.open_discovery")
    no_seed_issue(rig)
    go_repo(rig)
    open_repo(rig.e2e.gh, rig.ctx, "acme/forked-engine", topic="gitops", stars=30000, fork=True)
    open_repo(rig.e2e.gh, rig.ctx, "acme/busl-engine", topic="gitops", stars=20000, license={"spdx_id": "BUSL-1.1"})
    issue(rig.e2e.gh, rig.ctx, "acme/forked-engine", 1)
    issue(rig.e2e.gh, rig.ctx, "acme/busl-engine", 1)
    created = rig.cycle()
    assert [rig.upstream(c) for c in created] == [OPEN]
    c = rig.e2e.sc.gk.contribution(created[0])
    assert (c["issue_number"], c["source"], c["category"]) == (41, "discover", OPEN_CATEGORY)
    mid = await rig.card(created[0])
    body = text(rig.tg, mid)
    assert "🔎 open discovery · topic gitops · ★ 24,440" in body
    assert labels(rig.tg, mid)[-2:] == KEEP_ROW
    assert rig.e2e.pipeline_log() == ["kiro:vetter", "fetch:scout", "kiro:scout"]
    found = row(rig.ctx, OPEN)
    assert found is not None and (found["status"], found["source"], found["topic"]) == ("candidate", "search", "gitops")
    assert found["tried_at"] is not None and found["cooldown_until"] is not None
    rows = [r[0] for r in rig.ctx.conn.execute("SELECT repo FROM discovered_targets")]
    assert rows == [OPEN]  # rules 3-12 (fork, license) write no row ...
    filtered = {getattr(r, "repo", None): getattr(r, "reason", None) for r in caplog.records
                if getattr(r, "event", "") == "open_repo_filtered"}  # fmt: skip
    assert filtered.get("acme/forked-engine") and filtered.get("acme/busl-engine")  # ... only a DEBUG line
    rig.untouched()


# ---------------------------------------------------------------- scenario 2
async def test_keep_makes_it_a_kept_target_scanned_without_a_search(rig: Rig) -> None:
    no_seed_issue(rig)
    go_repo(rig)
    first = rig.cycle()
    mid = await rig.card(first[0])
    await rig.tap(mid, "⭐ Keep as target")
    await rig.tap(mid, "⭐ Keep this repo")
    kept = row(rig.ctx, OPEN)
    assert kept is not None and kept["status"] == "kept" and kept["decided_by"] == "tg:4242"
    assert labels(rig.tg, mid)[-2:] != KEEP_ROW
    issue(rig.e2e.gh, rig.ctx, OPEN, 42)
    mark, searches = len(rig.e2e.gh.requests), len(issue_searches(rig.e2e.gh, OPEN))
    second = rig.cycle()
    assert [(rig.upstream(c), rig.e2e.sc.gk.contribution(c)["issue_number"]) for c in second] == [(OPEN, 42)]
    during = rig.e2e.gh.requests[mark:]
    assert [x.path for x in during].count(f"/repos/{OPEN}") == 1  # the kept re-check
    assert any(x.path == f"/repos/{OPEN}/issues" for x in during)  # scanned like a seed target ...
    assert len(issue_searches(rig.e2e.gh, OPEN)) == searches  # ... without an issue search
    rechecked = row(rig.ctx, OPEN)
    assert rechecked is not None and rechecked["status"] == "kept" and rechecked["checked_at"] is not None
    mid2 = await rig.card(second[0])
    assert "⭐ kept target (open discovery) · ★ 24,440" in text(rig.tg, mid2)
    assert not set(KEEP_ROW) & set(labels(rig.tg, mid2))  # no fifth row for a kept repo
    rig.untouched()


# ---------------------------------------------------------------- scenario 3
async def test_never_blocks_the_repo_for_search_and_hermes(rig: Rig) -> None:
    no_seed_issue(rig)
    go_repo(rig)
    first = rig.cycle()
    mid = await rig.card(first[0])
    await rig.tap(mid, "🚫 Never this repo")
    assert "there is no Telegram undo for discovered repos" in text(rig.tg, mid)
    await rig.tap(mid, "🚫 Never this repo")
    blocked = row(rig.ctx, OPEN)
    assert blocked is not None and blocked["status"] == "never"
    assert rig.e2e.state(first[0]) == "PROPOSAL_REJECTED"
    issue(rig.e2e.gh, rig.ctx, OPEN, 42)
    searches = len(issue_searches(rig.e2e.gh, OPEN))
    assert rig.cycle() == []
    assert len(issue_searches(rig.e2e.gh, OPEN)) == searches  # not searched ...
    assert row(rig.ctx, OPEN) == blocked  # ... and not recorded
    status, answer = await ask(rig.ctx, f"https://github.com/{OPEN}/issues/42")
    assert (status, answer["error"]) == (409, "never_repo")
    assert rig.ctx.conn.execute("SELECT COUNT(*) FROM contributions").fetchone()[0] == 1
    rig.untouched()


# ---------------------------------------------------------------- scenario 4
async def test_hermes_suggestions_are_checked_and_gated(rig: Rig) -> None:
    no_seed_issue(rig)
    open_repo(rig.e2e.gh, rig.ctx, "acme/tiny-tool", topic="unrelated", stars=800)
    open_repo(rig.e2e.gh, rig.ctx, "acme/named-tool", topic="unrelated", stars=5000)
    issue(rig.e2e.gh, rig.ctx, "acme/named-tool", 5, label="enhancement")  # a named issue needs no label
    assert (await ask(rig.ctx, "https://github.com/acme/tiny-tool"))[0] == 202
    assert (await ask(rig.ctx, "https://github.com/acme/named-tool/issues/5"))[0] == 202
    created = rig.cycle()
    assert [rig.upstream(c) for c in created] == ["acme/named-tool"]
    tiny = row(rig.ctx, "acme/tiny-tool")
    assert tiny is not None and (tiny["status"], tiny["skip_reason"]) == ("skipped", "stars_below_min")
    await rig.tg.flush()
    assert any("Hermes suggestion acme/tiny-tool not used" in n for n in rig.e2e.notes())  # the FYI
    named = row(rig.ctx, "acme/named-tool")
    assert named is not None and (named["status"], named["source"]) == ("candidate", "hermes")
    cid = created[0]
    assert rig.e2e.sc.gk.contribution(cid)["source"] == "hermes_request"
    mid = await rig.card(cid)
    assert "🔎 suggested by Hermes · ★ 5,000" in text(rig.tg, mid)
    await rig.tap(mid, "✅ Start work")
    assert rig.e2e.state(cid) == "PROPOSED"  # one tap is not a decision
    await rig.tap(mid, "🛠 Confirm")
    assert rig.e2e.state(cid) == "PROPOSAL_APPROVED"
    rig.untouched()


# ---------------------------------------------------------------- scenario 5
async def test_a_search_rate_limit_defers_the_cycle(rig: Rig) -> None:
    go_repo(rig)  # the seed keeps its issue: one seed and one open candidate
    reset = int(rig.ctx.clock.now().timestamp()) + 600
    limited = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset), "x-ratelimit-resource": "search"}
    rig.e2e.gh.answers["GET /search/repositories"] = [(403, limited, {"message": "API rate limit exceeded"})]
    scheduler = rig.e2e.ready_scheduler()
    assert rig.cycle() == []
    assert scheduler.cycle_cut and scheduler.discover_after is not None
    assert rig.e2e.sc.load(od.OPEN_NOTE)["search_until"] == reset
    assert rig.ctx.conn.execute("SELECT COUNT(*) FROM contributions").fetchone()[0] == 0  # none without its check
    rig.ctx.clock.advance(seconds=601)  # type: ignore[attr-defined]
    created = rig.cycle()
    assert sorted(rig.upstream(c) for c in created) == [OPEN, SEED]
    rig.untouched()
