"""Floor holds (build-env design §5.2, ADR-0025): the discriminated ``HoldRequest`` (``egress_anomaly`` needs a
breach and no floor; ``egress_floor`` needs the floor and 0-3 breaches), the row stored with its reason under
migration 0010's CHECK, approvals invalidated with ``egress_floor``, the labels in both modules, the card
queued, a pre-0010 anomaly row still read back, and release for both reasons."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from ossbot.common.schemas import AnomalyHoldRequest, FloorHoldRequest, HoldRequest, HoldView, State
from ossbot.gatekeeper import approvals, db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.holds import view
from ossbot.gatekeeper.telegram.review import stale
from tests.conftest import UID_SANDBOX, insert_contribution, packaged_contribution
from tests.fakes.holds import floor_body, floor_request, hold_body, hold_request
from tests.unit.gatekeeper.api_helpers import client

SANDBOX = "uid:2002/sandbox"
pytestmark = pytest.mark.unit


def test_a_floor_hold_with_no_breach_is_accepted() -> None:
    req = floor_request()
    assert isinstance(req.root, FloorHoldRequest) and req.reason == "egress_floor" and req.breached == []
    assert req.floor.cls == "dst" and req.floor.target == "repo.maven.apache.org" and req.floor.dst == "10.0.0.1"
    assert isinstance(hold_request().root, AnomalyHoldRequest)


@pytest.mark.parametrize("cls", ["dst", "name", "code", "literal", "host", "proxy", "pub"])
def test_every_floor_class_parses(cls: str) -> None:
    target, dst = ("public-address", "-") if cls == "pub" else ("10.0.0.1", "10.0.0.1")
    assert floor_request(cls, target, dst).floor.cls == cls


@pytest.mark.parametrize("body", [
    {k: v for k, v in floor_body().items() if k != "floor"},  # egress_floor without floor
    hold_body(floor={"class": "dst", "target": "x.example.com", "dst": "-"}),  # egress_anomaly with a floor
    hold_body(breached=[]),  # egress_anomaly without a breach
    {k: v for k, v in hold_body().items() if k != "reason"},  # no reason at all
    floor_body(breached=["hosts", "upload", "download", "hosts"]),  # more than three breaches
    floor_body(floor={"class": "lan", "target": "x.example.com", "dst": "-"}),  # unknown class
    floor_body(floor={"class": "dst", "target": "Bad Host", "dst": "-"}),
    floor_body(floor={"class": "dst", "target": "x.example.com", "dst": "my-router"}),
    floor_body(floor={"class": "dst", "target": "x.example.com", "dst": "-", "extra": 1}),
])  # fmt: skip
def test_schema_refuses_bad_hold_bodies(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        HoldRequest.model_validate(body)


def test_floor_hold_stored_with_its_reason_and_invalidates_with_it(ctx: AppContext) -> None:
    cid, action = packaged_contribution(ctx)
    with db.transaction(ctx.conn):
        approval = ctx.create_approval(action, "test")
    seen: list[HoldView] = []
    ctx.holds.publish_hold = seen.append
    found, created = ctx.holds.hold(cid, floor_request("pub", "public-address", "-"), SANDBOX)
    assert created and found.reason == "egress_floor" and found.detail.floor.cls == "pub"
    row = db.one(ctx.conn, "SELECT reason, detail_json FROM contribution_holds WHERE id = ?", (found.id,))
    assert row is not None and row["reason"] == "egress_floor"
    assert json.loads(row["detail_json"])["floor"] == {"class": "pub", "target": "public-address", "dst": "-"}
    status = db.one(ctx.conn, "SELECT status, invalidated_reason FROM approvals WHERE id = ?", (approval,))
    assert status is not None and tuple(status) == ("invalidated", "egress_floor")
    assert [v.id for v in seen] == [found.id]  # the card is queued once
    held = [json.loads(r[0]) for r in ctx.conn.execute(
        "SELECT data_json FROM audit_events WHERE event = 'contribution_held'")]  # fmt: skip
    assert held[0]["reason"] == "egress_floor" and held[0]["invalidated"] == [approval]


def test_reason_labels_name_the_floor_hold() -> None:
    assert approvals.REASON_LABELS["egress_floor"] == "floor reach attempt: contribution held"
    assert stale.REASON_LABELS["egress_floor"] == ("floor reach attempt: contribution held", "none")


def test_a_pre_0010_anomaly_row_reads_back(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.VALIDATING)
    detail = json.dumps(hold_body(), sort_keys=True)  # exactly what 0007-era rows hold
    with db.transaction(ctx.conn):
        ctx.conn.execute("INSERT INTO contribution_holds (id, cid, reason, detail_json, held_by, held_at)"
                         " VALUES ('h_0000000001', ?, 'egress_anomaly', ?, 'x', 't')", (cid, detail))  # fmt: skip
    row = db.one(ctx.conn, "SELECT * FROM contribution_holds WHERE id = 'h_0000000001'")
    assert row is not None
    got = view(row)
    assert got.reason == "egress_anomaly" and isinstance(got.detail.root, AnomalyHoldRequest)
    assert got.detail.breached == ["upload"]


@pytest.mark.parametrize("make", [hold_request, floor_request])
def test_both_reasons_release_the_same_way(ctx: AppContext, make: Any) -> None:
    cid = insert_contribution(ctx, State.VALIDATING)
    ctx.holds.hold(cid, make(), SANDBOX)
    assert ctx.holds.is_held(cid)
    released = ctx.holds.release(cid, "tg:4242")
    assert released.released_by == "tg:4242" and not ctx.holds.is_held(cid)


async def test_the_endpoint_parses_the_discriminated_body(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.VALIDATING)
    async with client(ctx, UID_SANDBOX) as c:
        missing = await c.post(f"/v1/contributions/{cid}/hold", json={k: v for k, v in floor_body().items()
                                                                       if k != "floor"})  # fmt: skip
        ok = await c.post(f"/v1/contributions/{cid}/hold", json=floor_body("code", "api.telegram.org", "-"))
    assert missing.status_code == 400 and missing.json()["error"] == "schema_error"
    assert ok.status_code == 201
    assert ok.json()["reason"] == "egress_floor"
    assert ok.json()["detail"]["floor"] == {"class": "code", "target": "api.telegram.org", "dst": "-"}  # the wire name
