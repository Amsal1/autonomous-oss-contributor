"""Discovery preferences (CR-004 Amendment B; DESIGN §9.14): the only writer, ceiling keys refused,
audit + FYI + 60 s undo, precedence, and approved proposals left untouched."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from ossbot.common.clock import FakeClock
from ossbot.common.config import DiscoveryFilters
from ossbot.common.schemas import PrefsUpdate
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.discovery_prefs import PrefsRefused
from tests.conftest import UID_OPERATOR, UID_REQUESTER, UID_WORKER
from tests.unit.gatekeeper.api_helpers import client
from tests.unit.gatekeeper.test_proposals import scouting, start, submit

SRC = Path(__file__).resolve().parents[3] / "src" / "ossbot"
ACTOR = "uid:1000/operator"


def texts(ctx: AppContext) -> list[str]:
    return [json.loads(r[0])["text"] for r in ctx.conn.execute("SELECT payload_json FROM tg_outbox ORDER BY id")]


def test_defaults_come_from_policy(ctx: AppContext) -> None:
    eff = ctx.discovery_prefs.effective()
    assert eff.autonomous_max_class == "s" and eff.preferred_classes == ("xs", "s")
    assert eff.sources["autonomous_max_class"] == "policy.yaml" and not eff.never_repo


def test_set_audits_fyis_and_undoes_within_60s(ctx: AppContext, clock: FakeClock) -> None:
    out = ctx.discovery_prefs.update(PrefsUpdate(autonomous_max_class="m"), ACTOR)
    assert out["effective"]["autonomous_max_class"] == "m"
    assert any("auto size ≤ m" in t and "Undo within 60 s" in t for t in texts(ctx))
    ev = db.one(ctx.conn, "SELECT data_json FROM audit_events WHERE event = 'discovery_prefs_changed'")
    assert ev is not None and json.loads(ev[0])["after"]["autonomous_max_class"] == "m"
    clock.advance(seconds=30)
    ctx.discovery_prefs.undo(out["undo_id"], ACTOR)
    assert ctx.discovery_prefs.effective().autonomous_max_class == "s"
    with pytest.raises(PrefsRefused) as info:
        ctx.discovery_prefs.undo(out["undo_id"], ACTOR)
    assert info.value.code == "too_late"


def test_undo_after_60s_refused(ctx: AppContext, clock: FakeClock) -> None:
    out = ctx.discovery_prefs.update(PrefsUpdate(autonomous_max_class="l"), ACTOR)
    clock.advance(seconds=61)
    with pytest.raises(PrefsRefused) as info:
        ctx.discovery_prefs.undo(out["undo_id"], ACTOR)
    assert info.value.code == "too_late"
    assert ctx.discovery_prefs.effective().autonomous_max_class == "l"


@pytest.mark.parametrize("key", ["max_files", "max_changed_lines", "max_test_lines", "diff", "ceiling"])
def test_ceiling_keys_refused(ctx: AppContext, key: str) -> None:
    with pytest.raises(PrefsRefused) as info:
        ctx.discovery_prefs.update(PrefsUpdate(filters={key: 9999}), ACTOR)
    assert info.value.code == "ceiling_key"
    assert ctx.discovery_prefs.rows() == []


def test_unknown_and_invalid_filters_refused(ctx: AppContext) -> None:
    with pytest.raises(PrefsRefused, match="unknown_filter"):
        ctx.discovery_prefs.update(PrefsUpdate(filters={"bogus": 1}), ACTOR)
    with pytest.raises(PrefsRefused, match="bad_filter"):
        ctx.discovery_prefs.update(PrefsUpdate(filters={"validation_tier_max": "T9"}), ACTOR)
    with pytest.raises(PrefsRefused, match="unknown_filter"):  # never is per repo only
        ctx.discovery_prefs.update(PrefsUpdate(filters={"never": True}), ACTOR)


def test_repo_scope_needs_a_target_and_inherit(ctx: AppContext) -> None:
    with pytest.raises(PrefsRefused, match="not_a_target"):
        ctx.discovery_prefs.update(PrefsUpdate(scope="nobody/nothing", autonomous_max_class="m"), ACTOR)
    with pytest.raises(PrefsRefused, match="inherit_needs_repo"):
        ctx.discovery_prefs.update(PrefsUpdate(autonomous_max_class="inherit"), ACTOR)
    ctx.discovery_prefs.update(PrefsUpdate(scope="langfuse/langfuse", autonomous_max_class="xl"), ACTOR)
    assert ctx.discovery_prefs.effective("langfuse/langfuse").autonomous_max_class == "xl"
    assert ctx.discovery_prefs.effective("langfuse/langfuse-js").autonomous_max_class == "s"
    ctx.discovery_prefs.update(PrefsUpdate(scope="langfuse/langfuse", autonomous_max_class="inherit"), ACTOR)
    assert ctx.discovery_prefs.effective("langfuse/langfuse").autonomous_max_class == "s"


def test_filters_merge_per_scope(ctx: AppContext) -> None:
    ctx.discovery_prefs.update(PrefsUpdate(filters={"require_labels_any": ["good first issue"]}), ACTOR)
    ctx.discovery_prefs.update(PrefsUpdate(filters={"max_issue_age_days": 30}), ACTOR)
    eff = ctx.discovery_prefs.effective()
    assert eff.filters.require_labels_any == ["good first issue"] and eff.filters.max_issue_age_days == 30


def test_preferences_never_touch_an_approved_proposal(ctx: AppContext) -> None:
    """CR4-2n: a /size change after Start work keeps the approved class and proposal content."""
    cid = scouting(ctx)
    out = submit(ctx, cid)
    start(ctx, out["approval_id"])
    before = [tuple(r) for r in ctx.conn.execute("SELECT * FROM proposals")]
    approved = db.one(ctx.conn, "SELECT approved_class, state FROM contributions WHERE id = ?", (cid,))
    ctx.discovery_prefs.update(PrefsUpdate(autonomous_max_class="xs", filters={"allowed_kinds": ["docs"]}), ACTOR)
    assert [tuple(r) for r in ctx.conn.execute("SELECT * FROM proposals")] == before
    after = db.one(ctx.conn, "SELECT approved_class, state FROM contributions WHERE id = ?", (cid,))
    assert approved is not None and after is not None and tuple(after) == tuple(approved) == ("s", "PROPOSAL_APPROVED")


async def test_api_roles(ctx: AppContext) -> None:
    async with client(ctx, UID_WORKER) as c:
        assert (await c.get("/v1/discovery-prefs?repo=langfuse/langfuse")).status_code == 200
        assert (await c.put("/v1/discovery-prefs", json={"autonomous_max_class": "m"})).status_code == 403
    async with client(ctx, UID_REQUESTER) as c:
        assert (await c.get("/v1/discovery-prefs")).status_code == 403
    async with client(ctx, UID_OPERATOR) as c:
        resp = await c.put("/v1/discovery-prefs", json={"autonomous_max_class": "m"})
        assert resp.status_code == 200
        bad = await c.put("/v1/discovery-prefs", json={"filters": {"max_files": 500}})
        assert bad.status_code == 400 and bad.json()["error"] == "ceiling_key"
        undo = await c.post("/v1/discovery-prefs/undo", json={"undo_id": resp.json()["undo_id"]})
        assert undo.status_code == 200


# ------------------------------------------------------------- post-v1 open discovery (design §3.2)
def test_open_defaults_come_from_policy(ctx: AppContext) -> None:
    eff = ctx.discovery_prefs.effective()
    p = ctx.config.policy.discovery.open
    assert eff.open is not None and eff.open.enabled is True
    assert eff.open.topics == p.topics and eff.open.languages_preferred == p.languages_preferred
    assert eff.sources["open"] == "policy.yaml"
    assert eff.as_dict()["open"] == {"enabled": True, "topics": p.topics, "languages_preferred": p.languages_preferred}


def test_open_keys_at_global_scope(ctx: AppContext, clock: FakeClock) -> None:
    out = ctx.discovery_prefs.update(
        PrefsUpdate(filters={"open_enabled": False, "open_topics": ["gitops", "helm"], "max_issue_age_days": 30}),
        ACTOR,
    )
    langs = list(ctx.config.policy.discovery.open.languages_preferred)
    assert out["effective"]["open"] == {"enabled": False, "topics": ["gitops", "helm"], "languages_preferred": langs}
    eff = ctx.discovery_prefs.effective()
    assert eff.sources["open"] == "prefs *" and eff.filters.max_issue_age_days == 30
    # the filters part is exactly DiscoveryFilters (the runner validates it as such)
    assert set(out["effective"]["filters"]) == set(DiscoveryFilters.model_fields)
    DiscoveryFilters.model_validate(out["effective"]["filters"])
    repo = ctx.discovery_prefs.effective("langfuse/langfuse")  # a repo view with open keys set still works
    DiscoveryFilters.model_validate(repo.as_dict()["filters"])
    assert repo.open is not None and repo.open.enabled is False
    assert any("open discovery off" in t and "topics 2" in t and "filters max_issue_age_days" in t
               for t in texts(ctx))  # fmt: skip
    ctx.discovery_prefs.update(PrefsUpdate(filters={"open_languages_preferred": ["Go"]}), ACTOR)
    assert any("languages 1" in t for t in texts(ctx))
    second = ctx.discovery_prefs.update(PrefsUpdate(filters={"open_enabled": True}), ACTOR)
    clock.advance(seconds=10)
    ctx.discovery_prefs.undo(second["undo_id"], ACTOR)  # undo restores filters_json whole
    restored = ctx.discovery_prefs.effective()
    assert restored.open is not None and restored.open.enabled is False and restored.open.languages_preferred == ["Go"]


@pytest.mark.parametrize(
    "key",
    ["open_min_stars", "open_search_budget", "open_new_repos_per_cycle", "open_topics_per_cycle",
     "open_reserved_slots", "open_repo_cooldown_days", "open_pushed_within_days", "open_language_weight",
     "open_issue_labels"],
)  # fmt: skip
def test_open_counts_are_config_only(ctx: AppContext, key: str) -> None:
    for scope in ("*", "langfuse/langfuse"):
        with pytest.raises(PrefsRefused) as info:
            ctx.discovery_prefs.update(PrefsUpdate(scope=scope, filters={key: 99}), ACTOR)
        assert (info.value.status, info.value.code) == (400, "config_only")
    assert ctx.discovery_prefs.rows() == []


def test_open_key_check_order_and_validation(ctx: AppContext) -> None:
    with pytest.raises(PrefsRefused) as info:  # ceiling first
        ctx.discovery_prefs.update(PrefsUpdate(filters={"max_files": 1, "open_min_stars": 1}), ACTOR)
    assert info.value.code == "ceiling_key"
    with pytest.raises(PrefsRefused) as info:  # then config_only
        ctx.discovery_prefs.update(PrefsUpdate(filters={"open_min_stars": 1, "bogus": 1}), ACTOR)
    assert info.value.code == "config_only"
    with pytest.raises(PrefsRefused) as info:  # then unknown_filter
        ctx.discovery_prefs.update(PrefsUpdate(scope="langfuse/langfuse", filters={"bogus": 1, "open_enabled": True}),
                                   ACTOR)  # fmt: skip
    assert info.value.code == "unknown_filter"
    with pytest.raises(PrefsRefused) as info:  # then open_key_global_only
        ctx.discovery_prefs.update(PrefsUpdate(scope="langfuse/langfuse", filters={"open_enabled": True}), ACTOR)
    assert info.value.code == "open_key_global_only"
    for bad in ({"open_topics": []}, {"open_topics": ["Bad_Topic"]}, {"open_topics": ["a", "a"]},
                {"open_languages_preferred": ["Go", "go"]}, {"open_enabled": "yes"}):  # fmt: skip
        with pytest.raises(PrefsRefused) as info:
            ctx.discovery_prefs.update(PrefsUpdate(filters=bad), ACTOR)
        assert info.value.code == "bad_filter" and info.value.detail.startswith("open_"), bad
    assert ctx.discovery_prefs.rows() == []


async def test_open_keys_through_the_api(ctx: AppContext) -> None:
    async with client(ctx, UID_OPERATOR) as c:
        resp = await c.put("/v1/discovery-prefs", json={"filters": {"open_enabled": False}})
        assert resp.status_code == 200 and resp.json()["effective"]["open"]["enabled"] is False
        bad = await c.put("/v1/discovery-prefs", json={"filters": {"open_search_budget": 25}})
        assert (bad.status_code, bad.json()["error"]) == (400, "config_only")
    async with client(ctx, UID_WORKER) as c:
        doc = (await c.get("/v1/discovery-prefs")).json()["effective"]
        DiscoveryFilters.model_validate(doc["filters"])
        assert doc["open"]["enabled"] is False


def test_only_discovery_prefs_writes_the_prefs_tables() -> None:
    """The contract of PLAN item 29: no other module writes discovery_prefs(_undo)."""
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name == "discovery_prefs.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                sql = " ".join(node.value.split()).upper()
                for verb in ("INSERT INTO DISCOVERY_PREFS", "UPDATE DISCOVERY_PREFS", "DELETE FROM DISCOVERY_PREFS"):
                    if verb in sql:
                        offenders.append(f"{path.relative_to(SRC)}: {verb}")
    assert offenders == []
