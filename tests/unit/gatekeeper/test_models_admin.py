"""ModelsAdmin (DESIGN Â§16.2, Â§16.3): ruamel round-trip, atomic replace keeping the mode,
catalog-or-force, role references, new-model notifications and the refresh request."""

from __future__ import annotations

import json
import os
import re
import stat

import pytest

from ossbot.common.clock import FakeClock
from ossbot.common.config import parse_models
from ossbot.common.schemas import CatalogEntry
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.models_admin import ModelSetError
from tests.conftest import UID_OPERATOR, UID_REQUESTER, UID_WORKER
from tests.unit.gatekeeper.api_helpers import client

ACTOR = "uid:1000/operator"


def _texts(ctx: AppContext) -> list[str]:
    rows = ctx.conn.execute("SELECT payload_json FROM tg_outbox WHERE method = 'sendMessage' ORDER BY id").fetchall()
    return [json.loads(r[0])["text"] for r in rows]


def _upsert(ctx: AppContext, *ids: str, names: dict[str, str] | None = None) -> list[str]:
    names = names or {}
    return ctx.models.upsert_catalog([CatalogEntry(model_id=i, name=names.get(i)) for i in ids], "uid:2001/worker")


def test_set_keeps_comments_mode_and_leaves_no_temp(ctx: AppContext) -> None:
    path = ctx.models.path
    os.chmod(path, 0o664)
    _upsert(ctx, "claude-next")
    before = path.read_text(encoding="utf-8")
    diff = ctx.models.set_model("coder", "claude-next", "xhigh", False, ACTOR)
    after = path.read_text(encoding="utf-8")
    assert (
        re.search(r"^-\s+coder:\s*\{\s*model: default, effort: high\s*\}", diff, re.MULTILINE) and "claude-next" in diff
    )
    assert [line for line in before.splitlines() if line.lstrip().startswith("#")] == [
        line for line in after.splitlines() if line.lstrip().startswith("#")
    ]  # every comment kept
    assert "# CR-004 scout stage; inherits planner" in after
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o664
    assert not list(path.parent.glob(".models.yaml.tmp-*"))
    cfg = parse_models(path.read_bytes())
    assert cfg.roles.coder.model == "claude-next" and cfg.roles.coder.effort == "xhigh"
    ev = db.one(ctx.conn, "SELECT data_json FROM audit_events WHERE event = 'model_set'")
    assert ev is not None and json.loads(ev[0])["role"] == "coder"


def test_unknown_role_and_non_catalog_id_refused_without_force(ctx: AppContext) -> None:
    before = ctx.models.path.read_bytes()
    with pytest.raises(ModelSetError, match="unknown role"):
        ctx.models.set_model("janitor", "default", None, False, ACTOR)
    with pytest.raises(ModelSetError, match="not in the current Kiro model catalog"):
        ctx.models.set_model("coder", "nope-1", None, False, ACTOR)
    assert ctx.models.path.read_bytes() == before


def test_force_records_the_id(ctx: AppContext) -> None:
    ctx.models.set_model("scout", "custom-x", None, True, ACTOR)
    row = db.one(ctx.conn, "SELECT forced, forced_by FROM model_catalog WHERE model_id = 'custom-x'")
    assert row is not None and row["forced"] == 1 and row["forced_by"] == ACTOR
    assert parse_models(ctx.models.path.read_bytes()).roles.scout.model == "custom-x"
    assert db.one(ctx.conn, "SELECT 1 FROM audit_events WHERE event = 'model_forced'") is not None


def test_role_reference_allowed_and_cycles_refused(ctx: AppContext) -> None:
    ctx.models.set_model("vetter", "planner", None, False, ACTOR)
    assert parse_models(ctx.models.path.read_bytes()).roles.vetter.model == "planner"
    before = ctx.models.path.read_bytes()
    with pytest.raises(ModelSetError, match="cycle"):
        ctx.models.set_model("planner", "scout", None, False, ACTOR)
    with pytest.raises(ModelSetError, match="reference"):
        ctx.models.set_model("default", "coder", None, False, ACTOR)
    assert ctx.models.path.read_bytes() == before


def test_vision_needs_an_image_model_when_known(ctx: AppContext) -> None:
    entries = [
        CatalogEntry(model_id="text-only", input_types=["TEXT"]),
        CatalogEntry(model_id="sees", input_types=["TEXT", "IMAGE"]),
    ]
    ctx.models.upsert_catalog(entries, "uid:2001/worker")
    with pytest.raises(ModelSetError, match="images"):
        ctx.models.set_model("vision", "text-only", None, False, ACTOR)
    ctx.models.set_model("vision", "sees", None, False, ACTOR)
    ctx.models.set_model("coder", "text-only", None, False, ACTOR)  # only vision is checked


def test_first_fill_sends_one_summary_then_one_fyi_per_new_id(ctx: AppContext, clock: FakeClock) -> None:
    assert _upsert(ctx, "m-one", "m-two", "m-three") == ["m-one", "m-two", "m-three"]
    assert _texts(ctx) == ["Kiro catalog loaded: 3 models"]
    clock.advance(hours=1)
    _upsert(ctx, "m-one", "m-two", "m-four", "m-five", names={"m-four": "Model Four"})
    assert _texts(ctx)[1:] == ["New Kiro model: m-five", "New Kiro model: m-four (Model Four)"]
    clock.advance(hours=1)
    _upsert(ctx, "m-one", "m-four", "m-five")
    assert len(_texts(ctx)) == 3  # nothing new: no message
    rows = ctx.conn.execute("SELECT COUNT(*) FROM model_catalog WHERE notified = 0").fetchone()[0]
    assert rows == 0


def test_forced_then_seen_is_not_announced(ctx: AppContext) -> None:
    _upsert(ctx, "m-one")
    ctx.models.set_model("coder", "pre-release", None, True, ACTOR)
    _upsert(ctx, "m-one", "pre-release")
    assert [t for t in _texts(ctx) if not t.startswith("🧠")] == ["Kiro catalog loaded: 1 models"]


@pytest.mark.parametrize("role", ["coder", "test", "default"])
def test_every_switch_is_audited_and_announced_once(ctx: AppContext, role: str) -> None:
    _upsert(ctx, "m-one", "m-two")
    before = parse_models(ctx.models.path.read_bytes())
    old = before.default if role == "default" else getattr(before.roles, role)
    ctx.models.set_model(role, "m-two", "xhigh", False, ACTOR)
    fyis = [t for t in _texts(ctx) if t.startswith("🧠 Model:")]
    assert fyis == [f"🧠 Model: {role} {old.model} ({old.effort}) → m-two (xhigh) (by {ACTOR})"]
    assert db.one(ctx.conn, "SELECT 1 FROM audit_events WHERE event = 'model_set'") is not None
    ctx.models.set_model(role, "m-two", "xhigh", False, ACTOR)  # no change: audited, nothing to announce
    assert len([t for t in _texts(ctx) if t.startswith("🧠 Model:")]) == 1


def test_forced_switch_is_announced_as_forced(ctx: AppContext) -> None:
    ctx.models.set_model("test", "pre-release", None, True, ACTOR)
    fyis = [t for t in _texts(ctx) if t.startswith("🧠 Model:")]
    assert len(fyis) == 1 and "→ pre-release" in fyis[0] and fyis[0].endswith("forced, not in the catalog")


def test_notify_disabled_marks_without_sending(ctx: AppContext) -> None:
    path = ctx.models.path
    path.write_text(path.read_text(encoding="utf-8").replace("notify_new_models: true", "notify_new_models: false"))
    _upsert(ctx, "m-one")
    assert _texts(ctx) == []
    assert ctx.conn.execute("SELECT notified FROM model_catalog").fetchone()[0] == 1


async def test_refresh_request_roundtrip(ctx: AppContext) -> None:
    async with client(ctx, UID_WORKER) as c:
        assert (await c.post("/v1/models/refresh/claim")).json() == {"requested": False}
        assert (await c.post("/v1/models/refresh")).status_code == 403
    async with client(ctx, UID_OPERATOR) as c:
        resp = await c.post("/v1/models/refresh")
        assert resp.status_code == 202 and resp.json()["requested_at"]
        await c.post("/v1/models/refresh")  # idempotent: one pending request
        assert (await c.post("/v1/models/refresh/claim")).status_code == 403
    async with client(ctx, UID_REQUESTER) as c:
        assert (await c.post("/v1/models/refresh")).status_code == 403
    async with client(ctx, UID_WORKER) as c:
        assert (await c.post("/v1/models/refresh/claim")).json() == {"requested": True}
        assert (await c.post("/v1/models/refresh/claim")).json() == {"requested": False}


async def test_put_force_through_the_api(ctx: AppContext) -> None:
    async with client(ctx, UID_OPERATOR) as c:
        bad = await c.put("/v1/models/scout", json={"model": "not-listed"})
        assert bad.status_code == 400
        ok = await c.put("/v1/models/scout", json={"model": "not-listed", "force": True})
        assert ok.status_code == 200 and "not-listed" in ok.json()["diff"]
        catalog = (await c.get("/v1/models/catalog")).json()
    assert {"model_id": "not-listed", "forced": True} in catalog
