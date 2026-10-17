"""Catalog upsert POST /v1/models/catalog (R4-N3) and the model switch writer (§16.2)."""

from __future__ import annotations

import pytest

from ossbot.common.clock import FakeClock
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from tests.conftest import UID_OPERATOR, UID_REQUESTER, UID_SANDBOX, UID_WORKER
from tests.unit.gatekeeper.api_helpers import client


def count(ctx: AppContext) -> int:
    return int(ctx.conn.execute("SELECT COUNT(*) FROM model_catalog").fetchone()[0])


async def test_worker_upsert_inserts_updates_and_returns_new_ids(ctx: AppContext, clock: FakeClock) -> None:
    async with client(ctx, UID_WORKER) as c:
        first = await c.post("/v1/models/catalog", json={"models": [{"model_id": "m-one", "name": "One"},
                                                                    {"model_id": "m-two"}]})  # fmt: skip
        assert first.status_code == 200 and first.json() == {"new_ids": ["m-one", "m-two"]}
        clock.advance(hours=1)
        second = await c.post("/v1/models/catalog", json={"models": [{"model_id": "m-one", "name": "One v2"},
                                                                     {"model_id": "m-three"}]})  # fmt: skip
        assert second.json() == {"new_ids": ["m-three"]}
        catalog = (await c.get("/v1/models/catalog")).json()
    assert {e["model_id"] for e in catalog} == {"m-one", "m-three"}  # m-two is stale, row kept
    assert count(ctx) == 3
    row = db.one(ctx.conn, "SELECT display_name, first_seen, last_seen FROM model_catalog WHERE model_id = 'm-one'")
    assert row is not None and row["display_name"] == "One v2" and row["first_seen"] < row["last_seen"]


@pytest.mark.parametrize(
    "body",
    [
        {"models": [{"model_id": f"m{i}"} for i in range(201)]},
        {"models": []},
        {"models": [{"model_id": "-bad"}]},
        {"models": [{"model_id": "has space"}]},
        {"models": [{"model_id": "m", "forced": True}]},
        {"models": [{"model_id": "m", "name": 5}]},
        {"models": [{"model_id": "m", "name": "x" * 201}]},
        {"models": [{"model_id": "dup"}, {"model_id": "dup"}]},
    ],
)
async def test_invalid_upsert_is_400_and_writes_nothing(ctx: AppContext, body: dict[str, object]) -> None:
    async with client(ctx, UID_WORKER) as c:
        resp = await c.post("/v1/models/catalog", json=body)
    assert resp.status_code == 400
    assert count(ctx) == 0


async def test_forced_row_stays_forced(ctx: AppContext) -> None:
    ctx.models.set_model("coder", "custom-model-x", None, True, "uid:1000/operator")
    async with client(ctx, UID_WORKER) as c:
        await c.post("/v1/models/catalog", json={"models": [{"model_id": "m-one"}]})
        catalog = (await c.get("/v1/models/catalog")).json()
    assert {"model_id": "custom-model-x", "forced": True} in catalog
    row = db.one(ctx.conn, "SELECT forced FROM model_catalog WHERE model_id = 'custom-model-x'")
    assert row is not None and row[0] == 1


@pytest.mark.parametrize("uid", [UID_OPERATOR, UID_REQUESTER, UID_SANDBOX])
async def test_non_worker_upsert_is_403(ctx: AppContext, uid: int) -> None:
    async with client(ctx, uid) as c:
        resp = await c.post("/v1/models/catalog", json={"models": [{"model_id": "m"}]})
    assert resp.status_code == 403 and count(ctx) == 0


async def test_model_set_requires_catalog_id_unless_forced(ctx: AppContext) -> None:
    async with client(ctx, UID_WORKER) as c:
        await c.post("/v1/models/catalog", json={"models": [{"model_id": "claude-next"}]})
    path = ctx.paths.config_dir / "models.yaml"
    async with client(ctx, UID_OPERATOR) as c:
        bad = await c.put("/v1/models/coder", json={"model": "not-in-catalog"})
        assert bad.status_code == 400
        ok = await c.put("/v1/models/coder", json={"model": "claude-next", "effort": "xhigh"})
        assert ok.status_code == 200
        assert "claude-next" in ok.json()["diff"]
        assert (await c.get("/v1/models")).json()["roles"]["coder"] == {
            "model": "claude-next", "effort": "xhigh", "require_distinct_from": None,
        }  # fmt: skip
    text = path.read_text()
    assert "# Global model switch" in text  # comments kept (ruamel round-trip)
    assert not list(path.parent.glob(".models.yaml.tmp-*"))


async def test_model_set_unknown_role_rejected(ctx: AppContext) -> None:
    async with client(ctx, UID_OPERATOR) as c:
        assert (await c.put("/v1/models/janitor", json={"model": "default"})).status_code == 400
