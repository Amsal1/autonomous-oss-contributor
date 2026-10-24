"""Egress notes (build-env design §5.2, ADR-0025): sandboxd records them (role sandbox only), one row per run
(a repeat is 200), 404 for an unknown cid, 400 for a bad body, audited ``egress_note_recorded``; no state
transition, no approval invalidated, no Telegram message; the read-only list and the digest query."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from ossbot.common.clock import to_iso
from ossbot.common.schemas import EgressNoteRequest, State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.egress_notes import EgressNotes, NoteRefused
from tests.conftest import (
    UID_OPERATOR,
    UID_REQUESTER,
    UID_SANDBOX,
    UID_WORKER,
    insert_contribution,
    packaged_contribution,
)
from tests.fakes.holds import note_body
from tests.unit.gatekeeper.api_helpers import client

pytestmark = pytest.mark.unit


def _notes(ctx: AppContext) -> EgressNotes:
    return EgressNotes(ctx.conn, ctx.clock, ctx.audit)


def _outbox(ctx: AppContext) -> int:
    return int(ctx.conn.execute("SELECT COUNT(*) FROM tg_outbox").fetchone()[0])


def test_record_is_idempotent_per_run_and_changes_nothing_else(ctx: AppContext) -> None:
    cid, action = packaged_contribution(ctx)
    with db.transaction(ctx.conn):
        approval = ctx.create_approval(action, "test")
    before = _outbox(ctx)
    req = EgressNoteRequest.model_validate(note_body())
    first, created = _notes(ctx).record(cid, req, "uid:2002/sandbox")
    again, created_again = _notes(ctx).record(cid, req, "uid:2002/sandbox")
    assert created and not created_again and first == again
    assert first.kind == "volume" and first.summary.breached == ["hosts", "download"]
    assert db.one(ctx.conn, "SELECT state FROM contributions WHERE id = ?", (cid,))[0] == "AWAITING_APPROVAL"  # I13
    status = db.one(ctx.conn, "SELECT status FROM approvals WHERE id = ?", (approval,))
    assert status is not None and status[0] == "pending"  # nothing invalidated
    assert not ctx.holds.is_held(cid) and _outbox(ctx) == before  # no hold, no message at write time
    events = [json.loads(r[0]) for r in ctx.conn.execute(
        "SELECT data_json FROM audit_events WHERE event = 'egress_note_recorded'")]  # fmt: skip
    assert len(events) == 1 and events[0]["run"] == "validate-note01" and events[0]["kind"] == "volume"


def test_unknown_cid_refused(ctx: AppContext) -> None:
    with pytest.raises(NoteRefused) as info:
        _notes(ctx).record("c_0000000000", EgressNoteRequest.model_validate(note_body()), "x")
    assert (info.value.status, info.value.code) == (404, "unknown_cid")


@pytest.mark.parametrize("overrides", [
    {"kind": "hold"}, {"stage": "research"}, {"breached": ["hosts", "hosts"]}, {"breached": ["other"]},
    {"hosts": 1_000_001}, {"down_bytes": -1}, {"top_hosts": ["Evil Host"]}, {"notes": ["x" * 121]},
    {"notes": ["n"] * 21}, {"run_id": "Bad Run"}, {"extra": 1}, {"egress_mode": "wide"},
])  # fmt: skip
def test_schema_refuses_bad_bodies(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        EgressNoteRequest.model_validate(note_body(**overrides))


def test_a_floor_note_parses() -> None:
    req = EgressNoteRequest.model_validate(note_body(kind="floor_note", breached=[], notes=["loopback: lvh.me"]))
    assert req.kind == "floor_note" and req.notes == ["loopback: lvh.me"]


async def test_endpoint_only_for_sandbox(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.VALIDATING)
    for uid in (UID_WORKER, UID_REQUESTER, UID_OPERATOR):
        async with client(ctx, uid) as c:
            assert (await c.post(f"/v1/contributions/{cid}/egress-notes", json=note_body())).status_code == 403
    async with client(ctx, UID_SANDBOX) as c:
        first = await c.post(f"/v1/contributions/{cid}/egress-notes", json=note_body())
        again = await c.post(f"/v1/contributions/{cid}/egress-notes", json=note_body())
        bad = await c.post(f"/v1/contributions/{cid}/egress-notes", json=note_body(kind="hold"))
        unknown = await c.post("/v1/contributions/c_0000000000/egress-notes", json=note_body())
    assert first.status_code == 201 and again.status_code == 200 and first.json() == again.json()
    assert (bad.status_code, bad.json()["error"]) == (400, "schema_error")
    assert (unknown.status_code, unknown.json()["error"]) == (404, "unknown_cid")


async def test_list_is_read_only_for_worker_and_operator(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.VALIDATING)
    other = insert_contribution(ctx, State.VALIDATING)
    _notes(ctx).record(cid, EgressNoteRequest.model_validate(note_body()), "s")
    _notes(ctx).record(other, EgressNoteRequest.model_validate(note_body(run_id="fetch-note02", stage="fetch")), "s")
    async with client(ctx, UID_SANDBOX) as c:
        assert (await c.get("/v1/egress/notes")).status_code == 403
    for uid in (UID_WORKER, UID_OPERATOR):
        async with client(ctx, uid) as c:
            every = (await c.get("/v1/egress/notes")).json()
            mine = (await c.get(f"/v1/egress/notes?cid={cid}")).json()
        assert {n["run_id"] for n in every} == {"validate-note01", "fetch-note02"}
        assert [n["run_id"] for n in mine] == ["validate-note01"]


def test_since_window_for_the_digest(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.VALIDATING)
    _notes(ctx).record(cid, EgressNoteRequest.model_validate(note_body()), "s")
    now = ctx.clock.now()
    assert len(_notes(ctx).since(to_iso(now - timedelta(minutes=1)))) == 1
    assert _notes(ctx).since(to_iso(now + timedelta(minutes=1))) == []
