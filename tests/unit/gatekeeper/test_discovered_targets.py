"""Discovered targets (post-v1 open discovery, design §8.3, §8.5): every lifecycle row, the refusals,
audit and FYI rules, cooldowns, routing for config targets and the M1 resolution rule."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ossbot.common.clock import FakeClock, to_iso
from ossbot.common.config import KEPT_PRIORITY, OPEN_CATEGORY, OPEN_PRIORITY
from ossbot.common.schemas import DiscoveredFacts
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.discovered_targets import DiscoveredRefused

WORKER = "uid:2001/worker"
OPERATOR = "uid:1000/operator"
HERMES = "uid:2003/requester"
REPO = "openfaas/faas"  # not in config/targets.yaml


def facts(repo: str = REPO, **kw: Any) -> DiscoveredFacts:
    base: dict[str, Any] = {"repo": repo, "source": "search", "status": "candidate", "topic": "gitops",
                            "stars": 24440, "language": "Go", "license_spdx": "Apache-2.0",
                            "pushed_at": "2026-10-20T08:00:00Z", "ci_on_pr": "yes", "repo_score": 80.0}  # fmt: skip
    return DiscoveredFacts.model_validate({**base, **kw})


def skipped(repo: str = REPO, reason: str = "no_contributing", **kw: Any) -> DiscoveredFacts:
    return facts(repo, status="skipped", skip_reason=reason, **kw)


def events(ctx: AppContext, name: str) -> list[dict[str, Any]]:
    rows = ctx.conn.execute("SELECT data_json FROM audit_events WHERE event = ? ORDER BY seq", (name,))
    return [json.loads(r[0]) for r in rows]


def texts(ctx: AppContext) -> list[str]:
    return [json.loads(r[0])["text"] for r in ctx.conn.execute("SELECT payload_json FROM tg_outbox ORDER BY id")]


def refused(fn: Any, *args: Any) -> DiscoveredRefused:
    with pytest.raises(DiscoveredRefused) as info:
        fn(*args)
    return info.value


def contribution(ctx: AppContext, repo: str, state: str = "FETCHING", cid: str = "c_0000000001",
                 category: str = OPEN_CATEGORY) -> None:  # fmt: skip
    now = to_iso(ctx.clock.now())
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO contributions (id, upstream, issue_number, source, category, state, created_at, updated_at)"
            " VALUES (?, ?, 7, 'discover', ?, ?, ?, ?)",
            (cid, repo, category, state, now, now),
        )


# ------------------------------------------------------------------ worker rows
def test_new_candidate_recorded_once(ctx: AppContext) -> None:
    v = ctx.discovered.record(facts(), WORKER)
    assert (v.status, v.source, v.topic, v.stars, v.egress_strict) == ("candidate", "search", "gitops", 24440, False)
    assert v.tried_at is None and v.cooldown_until is None
    ctx.discovered.record(facts(stars=25000), WORKER)  # carry-over refresh: no new audit row
    assert len(events(ctx, "discovered_target_recorded")) == 1
    assert ctx.discovered.get(REPO).stars == 25000  # type: ignore[union-attr]
    assert texts(ctx) == []  # no FYI for search hits


def test_tried_sets_cooldown(ctx: AppContext, clock: FakeClock) -> None:
    ctx.discovered.record(facts(), WORKER)
    v = ctx.discovered.record(facts(tried=True), WORKER)
    assert v.tried_at == to_iso(clock.now())
    days = ctx.config.policy.discovery.open.repo_cooldown_days
    assert v.cooldown_until is not None and v.cooldown_until > v.tried_at and days == 14


def test_skip_sets_reason_and_cooldown_audits_on_change_only(ctx: AppContext, clock: FakeClock) -> None:
    v = ctx.discovered.record(skipped(), WORKER)
    assert v.status == "skipped" and v.skip_reason == "no_contributing" and v.cooldown_until is not None
    assert len(events(ctx, "discovered_target_skipped")) == 1
    assert refused(ctx.discovered.record, skipped(), WORKER).code == "cooling"
    clock.advance(days=15)
    ctx.discovered.record(skipped(), WORKER)  # same reason after the cooldown: no new audit row
    assert len(events(ctx, "discovered_target_skipped")) == 1
    clock.advance(days=15)
    ctx.discovered.record(skipped(reason="egress_extra_needed:a.io"), WORKER)
    assert len(events(ctx, "discovered_target_skipped")) == 2
    clock.advance(days=15)
    v = ctx.discovered.record(facts(), WORKER)  # skipped (cooldown passed) -> candidate
    assert v.status == "candidate" and v.skip_reason is None
    assert texts(ctx) == []  # FYI only for Hermes skips


def test_source_never_changes_and_topic_filled_once(ctx: AppContext, clock: FakeClock) -> None:
    ctx.discovered.suggest(REPO, None, HERMES)
    v = ctx.discovered.record(facts(source="search", topic=None), WORKER)
    assert v.source == "hermes" and v.topic is None and v.status == "candidate"
    v = ctx.discovered.record(facts(topic="gitops"), WORKER)
    assert v.topic == "gitops"
    v = ctx.discovered.record(facts(topic="kubernetes"), WORKER)
    assert v.topic == "gitops" and v.source == "hermes"


def test_default_branch_commit_stored_and_inactive_skip(ctx: AppContext) -> None:
    """Owner addition (2026-10-21): the default branch's latest commit is a stored fact; a stale default
    branch is a recorded skip with the existing cooldown."""
    v = ctx.discovered.record(facts(default_branch_commit_at="2026-10-01T00:00:00Z"), WORKER)
    assert v.default_branch_commit_at == "2026-10-01T00:00:00Z"
    v = ctx.discovered.record(skipped("o/stale", reason="inactive_default_branch",
                                      default_branch_commit_at="2026-01-01T00:00:00Z"), WORKER)  # fmt: skip
    assert (v.status, v.skip_reason, v.default_branch_commit_at) == (
        "skipped", "inactive_default_branch", "2026-01-01T00:00:00Z")  # fmt: skip
    assert v.cooldown_until is not None


def test_hermes_skip_sends_one_fyi(ctx: AppContext) -> None:
    ctx.discovered.suggest(REPO, 5, HERMES)
    ctx.discovered.record(skipped(reason="stars_below_min", source="hermes"), WORKER)
    assert texts(ctx) == [f"🔎 Hermes suggestion {REPO} not used: it has fewer stars than the minimum."]


def test_kept_check_pass_and_fail(ctx: AppContext, clock: FakeClock) -> None:
    ctx.discovered.decide(REPO, "keep", OPERATOR)
    v = ctx.discovered.get(REPO)
    assert v is not None and v.source == "owner" and v.stars is None and v.checked_at is None
    v = ctx.discovered.record(skipped(kept_check=True, reason="archived"), WORKER)
    assert (v.status, v.skip_reason, v.checked_at) == ("kept", "archived", to_iso(clock.now()))
    assert v.stars == 24440 and v.cooldown_until is None
    clock.advance(days=1)
    v = ctx.discovered.record(facts(kept_check=True), WORKER)
    assert (v.status, v.skip_reason, v.source) == ("kept", None, "owner")
    assert len(events(ctx, "discovered_target_checked")) == 2


def test_worker_refusals(ctx: AppContext) -> None:
    assert refused(ctx.discovered.record, facts(kept_check=True), WORKER).code == "not_kept"
    ctx.discovered.record(facts(), WORKER)
    assert refused(ctx.discovered.record, facts(kept_check=True), WORKER).code == "not_kept"
    assert refused(ctx.discovered.record, facts("langfuse/langfuse"), WORKER).code == "is_target"
    denied = refused(ctx.discovered.record, facts("LineageOS/android"), WORKER)
    assert (denied.status, denied.code) == (400, "denied")
    # an owner in profile.forbidden_logins (test fixture) and one only in the policy floor
    assert refused(ctx.discovered.record, facts("test-work-login/x"), WORKER).code == "denied"
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO policy_floor (kind, value, added_at) VALUES ('forbidden_login', 'old-login', 't')"
        )
    assert refused(ctx.discovered.record, facts("old-login/x"), WORKER).code == "denied"
    assert ctx.discovered.get("old-login/x") is None
    ctx.discovered.decide(REPO, "keep", OPERATOR)
    assert refused(ctx.discovered.record, facts(), WORKER).code == "is_kept"


def test_never_is_sticky(ctx: AppContext) -> None:
    ctx.discovered.record(facts(), WORKER)
    ctx.discovered.decide(REPO, "never", OPERATOR)
    before = ctx.discovered.get(REPO)
    err = refused(ctx.discovered.record, facts(stars=1), WORKER)
    assert (err.status, err.code) == (409, "never_repo")
    assert refused(ctx.discovered.suggest, REPO, None, HERMES).code == "never_repo"
    assert ctx.discovered.get(REPO) == before


# ------------------------------------------------------------- owner decisions
def test_keep_unseen_repo_by_operator(ctx: AppContext) -> None:
    out = ctx.discovered.decide("fluxcd/flux2", "keep", OPERATOR)
    assert out["view"]["status"] == "kept" and out["view"]["source"] == "owner"
    assert out["view"]["decided_by"] == OPERATOR
    assert events(ctx, "discovered_target_kept")[0]["repo"] == "fluxcd/flux2"
    assert any(t.startswith("⭐ Kept fluxcd/flux2") for t in texts(ctx))


def test_keep_clears_cooldown_and_checked(ctx: AppContext) -> None:
    ctx.discovered.record(skipped(), WORKER)
    v = ctx.discovered.decide(REPO, "keep", OPERATOR)["view"]
    assert (v["status"], v["cooldown_until"], v["checked_at"], v["source"]) == ("kept", None, None, "search")


def test_never_unblock_unkeep(ctx: AppContext) -> None:
    ctx.discovered.decide("fluxcd/flux2", "never", OPERATOR)  # (none) -> never
    assert ctx.discovered.get("fluxcd/flux2").status == "never"  # type: ignore[union-attr]
    v = ctx.discovered.decide("fluxcd/flux2", "unblock", OPERATOR)["view"]
    assert v["status"] == "suggested"  # no facts
    ctx.discovered.record(facts(), WORKER)
    ctx.discovered.decide(REPO, "never", OPERATOR)
    v = ctx.discovered.decide(REPO, "unblock", OPERATOR)["view"]
    assert v["status"] == "candidate" and v["cooldown_until"] is None
    assert refused(ctx.discovered.decide, REPO, "unblock", OPERATOR).code == "not_never"
    assert refused(ctx.discovered.decide, REPO, "unkeep", OPERATOR).code == "not_kept"
    ctx.discovered.decide(REPO, "keep", OPERATOR)
    assert ctx.discovered.decide(REPO, "unkeep", OPERATOR)["view"]["status"] == "candidate"
    for name in ("never", "unblocked", "unkept"):
        assert events(ctx, f"discovered_target_{name}"), name


def test_strict_on_off(ctx: AppContext) -> None:
    assert refused(ctx.discovered.decide, REPO, "strict", OPERATOR).code == "not_found"
    ctx.discovered.record(facts(), WORKER)
    assert ctx.discovered.decide(REPO, "strict", OPERATOR)["view"]["egress_strict"] is True
    ctx.discovered.decide(REPO, "strict", OPERATOR)  # idempotent: no second audit row
    assert ctx.discovered.decide(REPO, "unstrict", OPERATOR)["view"]["egress_strict"] is False
    assert [e["egress_strict"] for e in events(ctx, "discovered_target_egress")] == [True, False]
    assert sum(t.startswith("🔒") for t in texts(ctx)) == 2


def test_config_targets_route_or_refuse(ctx: AppContext) -> None:
    out = ctx.discovered.decide("langfuse/langfuse", "never", OPERATOR)
    assert out["routed"] == "discovery_prefs"
    assert ctx.discovery_prefs.effective("langfuse/langfuse").never_repo
    ctx.discovered.decide("langfuse/langfuse", "unblock", OPERATOR)
    assert not ctx.discovery_prefs.effective("langfuse/langfuse").never_repo
    assert refused(ctx.discovered.decide, "langfuse/langfuse", "keep", OPERATOR).code == "already_target"
    assert refused(ctx.discovered.decide, "langfuse/langfuse", "strict", OPERATOR).code == "is_target"
    assert ctx.discovered.get("langfuse/langfuse") is None
    assert refused(ctx.discovered.decide, "LineageOS/android", "keep", OPERATOR).code == "denied"


# ----------------------------------------------------------------- reads
def test_excluded_and_list(ctx: AppContext, clock: FakeClock) -> None:
    ctx.discovered.record(facts("a/queued"), WORKER)  # untried candidate
    ctx.discovered.record(facts("a/tried"), WORKER)
    ctx.discovered.record(facts("a/tried", tried=True), WORKER)  # in cooldown
    ctx.discovered.record(skipped("a/skipped"), WORKER)
    ctx.discovered.decide("a/kept", "keep", OPERATOR)
    ctx.discovered.decide("a/never", "never", OPERATOR)
    ctx.discovered.suggest("a/suggested", None, HERMES)
    assert ctx.discovered.excluded() == ["a/kept", "a/never", "a/queued", "a/skipped", "a/tried"]
    clock.advance(days=15)
    assert ctx.discovered.excluded() == ["a/kept", "a/never", "a/queued"]
    assert [v.repo for v in ctx.discovered.list("candidate", untried=True)] == ["a/queued"]
    assert len(ctx.discovered.list()) == 6 and len(ctx.discovered.list(limit=2)) == 2


# ------------------------------------------------------------- resolution (§8.5)
def test_effective_target_rules(ctx: AppContext) -> None:
    cfg = ctx.discovered.effective_target("langfuse/langfuse")
    assert cfg is not None and cfg.category != OPEN_CATEGORY
    assert ctx.discovered.effective_target(REPO) is None
    ctx.discovered.record(facts(), WORKER)
    t = ctx.discovered.effective_target(REPO.upper())
    assert t is not None and (t.repo, t.category, t.priority, t.egress) == (REPO, OPEN_CATEGORY, OPEN_PRIORITY, "mode")
    ctx.discovered.decide(REPO, "strict", OPERATOR)
    ctx.discovered.decide(REPO, "keep", OPERATOR)
    t = ctx.discovered.effective_target(REPO)
    assert t is not None and t.priority == KEPT_PRIORITY and t.egress == "strict"
    ctx.discovered.decide(REPO, "never", OPERATOR)
    assert ctx.discovered.effective_target(REPO) is not None  # in-flight items keep working
    ctx.discovered.suggest("a/suggested", None, HERMES)
    assert ctx.discovered.effective_target("a/suggested") is None
    ctx.discovered.record(skipped("a/skipped"), WORKER)
    assert ctx.discovered.effective_target("a/skipped") is None


def test_m1_skipped_repo_with_a_contribution_still_resolves(ctx: AppContext, clock: FakeClock) -> None:
    """Review-3 M1: an in-flight contribution keeps its target when its row moves to skipped."""
    ctx.discovered.record(facts(tried=True), WORKER)
    contribution(ctx, REPO)
    clock.advance(days=15)
    ctx.discovered.record(skipped(), WORKER)  # candidate -> skipped (a later search re-check failed)
    assert ctx.discovered.get(REPO).status == "skipped"  # type: ignore[union-attr]
    t = ctx.discovered.effective_target(REPO)
    assert t is not None and t.repo == REPO and t.category == OPEN_CATEGORY
    assert refused(ctx.discovered.creatable_target, REPO).code == "not_a_target"
    ctx.discovered.record(skipped("o/other"), WORKER)  # no contribution: not resolvable
    assert ctx.discovered.effective_target("o/other") is None
    contribution(ctx, "o/norow", cid="c_0000000002")  # a contribution without a row still resolves
    t = ctx.discovered.effective_target("o/norow")
    assert t is not None and t.priority == OPEN_PRIORITY
    # a contribution left over from a repo removed from targets.yaml (config category) does not resolve
    contribution(ctx, "o/removed", cid="c_0000000003", category="iac")
    assert ctx.discovered.effective_target("o/removed") is None


def test_creatable_target(ctx: AppContext) -> None:
    assert refused(ctx.discovered.creatable_target, REPO).code == "not_a_target"
    ctx.discovered.record(facts(), WORKER)
    assert ctx.discovered.creatable_target(REPO).priority == OPEN_PRIORITY
    ctx.discovered.decide(REPO, "keep", OPERATOR)
    assert ctx.discovered.creatable_target(REPO).priority == KEPT_PRIORITY
    ctx.discovered.decide(REPO, "never", OPERATOR)
    err = refused(ctx.discovered.creatable_target, REPO)
    assert (err.status, err.code) == (400, "never_repo")


def test_open_pr_count(ctx: AppContext) -> None:
    contribution(ctx, REPO, "AWAITING_APPROVAL")
    contribution(ctx, REPO.upper(), "FETCHING", cid="c_0000000002")
    assert ctx.discovered.open_pr_count(REPO) == 1
