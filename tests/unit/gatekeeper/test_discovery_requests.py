"""Hermes suggestions through ``POST /v1/discovery-requests`` (design §11; review-3 M2/M3, plan D1).

Untrusted input: only the slug and the issue number are stored; schema errors are 400 ``schema_error``
without an echo of the input; nothing here approves anything."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ossbot.common.schemas import DiscoveredFacts, State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from tests.conftest import UID_OPERATOR, UID_REQUESTER, UID_WORKER, insert_contribution
from tests.unit.gatekeeper.api_helpers import client

REPO = "openfaas/faas"  # not in config/targets.yaml
URL = f"https://github.com/{REPO}"
OPERATOR = "uid:1000/operator"


async def ask(ctx: AppContext, url: str, uid: int = UID_REQUESTER) -> httpx.Response:
    async with client(ctx, uid) as c:
        return await c.post("/v1/discovery-requests", json={"url": url})


def record(ctx: AppContext, repo: str = REPO, **kw: Any) -> None:
    facts = {"repo": repo, "source": "search", "status": "candidate", "stars": 25000, **kw}
    ctx.discovered.record(DiscoveredFacts.model_validate(facts), "uid:2001/worker")


def contributions(ctx: AppContext) -> list[dict[str, Any]]:
    return [dict(r) for r in ctx.conn.execute("SELECT upstream, issue_number, source FROM contributions")]


async def test_config_target_issue_unchanged_and_repo_url_refused(ctx: AppContext) -> None:
    resp = await ask(ctx, "https://github.com/langfuse/langfuse/issues/9")
    assert resp.status_code == 201 and resp.json()["cid"].startswith("c_")
    resp = await ask(ctx, "https://github.com/langfuse/langfuse")
    assert (resp.status_code, resp.json()) == (400, {"error": "already_a_target"})
    resp = await ask(ctx, "https://github.com/tobenwarrior/hermes-kiro-provider/issues/1")
    assert (resp.status_code, resp.json()["error"]) == (400, "not_an_enabled_target")


async def test_unknown_repo_becomes_a_suggestion(ctx: AppContext) -> None:
    resp = await ask(ctx, f"{URL}/issues/42")
    assert (resp.status_code, resp.json()) == (202, {"queued": True, "repo": REPO, "status": "suggested"})
    row = ctx.discovered.get(REPO)
    assert row is not None and (row.source, row.status, row.suggested_issue) == ("hermes", "suggested", 42)
    assert contributions(ctx) == []
    ev = db.one(ctx.conn, "SELECT actor, data_json FROM audit_events WHERE event = 'discovered_target_suggested'")
    assert ev is not None and ev["actor"] == "uid:2003/requester"
    assert json.loads(ev["data_json"]) == {"repo": REPO, "issue": 42}
    resp = await ask(ctx, URL)  # a repo URL again: still suggested, no issue
    assert resp.status_code == 202 and ctx.discovered.get(REPO).suggested_issue is None  # type: ignore[union-attr]


async def test_only_slug_and_number_are_stored(ctx: AppContext) -> None:
    await ask(ctx, f"{URL}/issues/7/")
    dumped = " ".join(str(v) for r in ctx.conn.execute("SELECT * FROM discovered_targets") for v in tuple(r))
    assert "https://" not in dumped and "/issues/" not in dumped


async def test_discovered_candidate_and_kept(ctx: AppContext) -> None:
    record(ctx)
    resp = await ask(ctx, URL)
    assert (resp.status_code, resp.json()) == (200, {"repo": REPO, "status": "candidate"})
    resp = await ask(ctx, f"{URL}/issues/5")
    assert resp.status_code == 201
    assert contributions(ctx) == [{"upstream": REPO, "issue_number": 5, "source": "hermes_request"}]
    ctx.discovered.decide("fluxcd/flux2", "keep", OPERATOR)
    resp = await ask(ctx, "https://github.com/fluxcd/flux2")
    assert resp.json() == {"repo": "fluxcd/flux2", "status": "kept"}


@pytest.mark.parametrize("state", ["AWAITING_APPROVAL", "APPROVED", "SUBMITTING", "SUBMITTED", "WATCHING"])
async def test_d1_one_open_pr_per_discovered_repo(ctx: AppContext, state: str) -> None:
    """Plan D1 (review-3 M2): a discovered repo with our open PR gets no new Hermes contribution."""
    record(ctx)
    insert_contribution(ctx, State(state), upstream=REPO, category="open-discovery")
    before = contributions(ctx)
    resp = await ask(ctx, f"{URL}/issues/77")
    assert (resp.status_code, resp.json()) == (409, {"error": "repo_has_open_pr"})
    assert contributions(ctx) == before


async def test_d1_other_states_do_not_count(ctx: AppContext) -> None:
    record(ctx)
    insert_contribution(ctx, upstream=REPO, category="open-discovery", issue_number=1)  # DISCOVERED
    assert (await ask(ctx, f"{URL}/issues/78")).status_code == 201


async def test_never_skipped_and_denied(ctx: AppContext) -> None:
    ctx.discovered.decide(REPO, "never", OPERATOR)
    resp = await ask(ctx, URL)
    assert (resp.status_code, resp.json()["error"]) == (409, "never_repo")
    record(ctx, "o/skipped", status="skipped", skip_reason="no_contributing")
    resp = await ask(ctx, "https://github.com/o/skipped/issues/3")
    assert (resp.status_code, resp.json()["error"], resp.json()["detail"]) == (409, "skipped", "no_contributing")
    resp = await ask(ctx, "https://github.com/LineageOS/android")
    assert (resp.status_code, resp.json()["error"]) == (400, "denied")
    resp = await ask(ctx, "https://github.com/test-work-login/x")
    assert (resp.status_code, resp.json()["error"]) == (400, "denied")


async def test_skipped_past_its_cooldown_is_suggested_again(ctx: AppContext) -> None:
    record(ctx, status="skipped", skip_reason="no_contributing")
    ctx.clock.advance(days=15)  # type: ignore[attr-defined]
    resp = await ask(ctx, URL)
    assert resp.status_code == 202
    row = ctx.discovered.get(REPO)
    assert row is not None and (row.status, row.source, row.skip_reason) == ("suggested", "search", None)


async def test_limits(ctx: AppContext) -> None:
    for i in range(10):
        assert (await ask(ctx, f"https://github.com/o/r{i}")).status_code == 202
    resp = await ask(ctx, "https://github.com/o/r10")
    assert (resp.status_code, resp.json()["error"]) == (429, "suggestion_limit")
    assert ctx.discovered.get("o/r10") is None
    ctx.clock.advance(hours=25)  # type: ignore[attr-defined]
    for i in range(10, 20):  # 20 suggested rows in total
        assert (await ask(ctx, f"https://github.com/o/r{i}")).status_code == 202
    ctx.clock.advance(hours=25)  # type: ignore[attr-defined]
    resp = await ask(ctx, "https://github.com/o/r20")
    assert (resp.status_code, resp.json()["error"]) == (429, "suggestion_limit")


@pytest.mark.parametrize(
    "url",
    [
        f"{URL}/pull/1",
        f"{URL}/issues/1#issuecomment-2",
        f"{URL}/issues/1?x=<script>",
        f"{URL}/issues/1\n",
        f"{URL}/issues/1/../../x",
        f"https://github.com/{REPO}@evil.io/issues/1",
        "https://github.com/-bad/repo",
        "https://github.com.evil.io/o/r",
        "javascript:alert(1)",
    ],
)
async def test_hostile_urls_refused_without_echo(ctx: AppContext, url: str) -> None:
    resp = await ask(ctx, url)
    assert resp.status_code == 400 and resp.json()["error"] == "schema_error"
    assert url.strip() not in resp.text
    assert contributions(ctx) == [] and ctx.discovered.list() == []


async def test_roles(ctx: AppContext) -> None:
    assert (await ask(ctx, URL, UID_OPERATOR)).status_code == 202
    assert (await ask(ctx, "https://github.com/o/w", UID_WORKER)).status_code == 403
