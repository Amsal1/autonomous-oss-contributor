"""Switching to ``sandbox_full`` (build-env design §9.2, §9.3, AC 9 control half): the mode flag, then implement
network raised to ``auto`` in its own transaction, two audits, one FYI; a failed raise keeps the mode and says so;
``raise_implement`` only with ``sandbox_full`` (422); ``sandbox_full`` only when the snapshot has it (409
``mode_not_in_snapshot``); leaving ``sandbox_full`` keeps the setting; nothing is invalidated."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.control import EgressModeRefused, ImplementRaiseFailed
from tests.conftest import UID_OPERATOR, UID_WORKER, packaged_contribution
from tests.unit.gatekeeper.api_helpers import client
from tests.unit.gatekeeper.decision_helpers import install_sources, proposal_card, proposed_contribution, skill_card
from tests.unit.gatekeeper.test_authz import outbox_texts
from tests.unit.gatekeeper.test_control import _audits, _rows

ACTOR = "tg:4242"


def _switch(ctx: AppContext, mode: str, *, raise_implement: bool = False) -> str:
    egress = ctx.config.egress
    return ctx.control.set_egress_mode(mode, ACTOR, egress.mode_default, egress=egress,  # type: ignore[arg-type]
                                       raise_implement=raise_implement)  # fmt: skip


@pytest.mark.parametrize("start", ["off", "on_request"])
def test_full_raises_implement_network_with_two_audits_and_one_fyi(ctx: AppContext, start: str) -> None:
    default = ctx.config.egress.mode_default
    ctx.control.set_implement_network(start, ACTOR, default)  # type: ignore[arg-type]
    before = len(outbox_texts(ctx))
    assert _switch(ctx, "sandbox_full", raise_implement=True) == "open"
    assert ctx.control.egress_mode(default) == "sandbox_full" and ctx.control.implement_network() == "auto"
    assert _audits(ctx, "egress_mode_changed")[-1] == {"from": "open", "to": "sandbox_full"}
    assert _audits(ctx, "implement_network_changed")[-1] == {"from": start, "to": "auto", "egress_mode": "sandbox_full"}
    new = outbox_texts(ctx)[before:]
    assert [t for t in new if "Egress mode" in t or "Implement network" in t] == [
        f"🌐 Egress mode open → sandbox_full by {ACTOR}; implement network {start} → auto"
    ]


def test_full_with_auto_already_writes_one_audit(ctx: AppContext) -> None:
    default = ctx.config.egress.mode_default
    ctx.control.set_implement_network("auto", ACTOR, default)
    seen = len(_audits(ctx, "implement_network_changed"))
    _switch(ctx, "sandbox_full", raise_implement=True)
    assert len(_audits(ctx, "implement_network_changed")) == seen
    assert any("Egress mode open → sandbox_full" in t and "fetch/validate +web_public" in t for t in outbox_texts(ctx))


def test_full_without_raise_keeps_the_setting(ctx: AppContext) -> None:
    _switch(ctx, "sandbox_full")
    assert ctx.control.implement_network() == "off"


def test_leaving_full_never_changes_implement_network(ctx: AppContext) -> None:
    _switch(ctx, "sandbox_full", raise_implement=True)
    for mode in ("sandbox_open", "open", "allowlist"):
        _switch(ctx, mode)
        assert ctx.control.implement_network() == "auto"


def test_a_failed_raise_keeps_the_mode_and_tells_the_caller(ctx: AppContext, monkeypatch: pytest.MonkeyPatch,
                                                           caplog: pytest.LogCaptureFixture) -> None:  # fmt: skip
    def broken(*args: Any) -> str:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(ctx.control, "_write_implement_network", broken)
    with caplog.at_level("ERROR"), pytest.raises(ImplementRaiseFailed) as info:
        _switch(ctx, "sandbox_full", raise_implement=True)
    assert (info.value.previous_mode, info.value.implement_previous) == ("open", "off")
    assert "database is locked" in info.value.error
    assert ctx.control.egress_mode(ctx.config.egress.mode_default) == "sandbox_full"  # the main intent stays
    assert ctx.control.implement_network() == "off"
    assert [r for r in caplog.records if getattr(r, "event", None) == "implement_network_raise_failed"]
    assert any("Egress mode open → sandbox_full" in t for t in outbox_texts(ctx))


@pytest.mark.parametrize("mode", ["allowlist", "research", "open", "sandbox_open"])
def test_raise_implement_only_with_full(ctx: AppContext, mode: str) -> None:
    with pytest.raises(EgressModeRefused) as info:
        _switch(ctx, mode, raise_implement=True)
    assert (info.value.status, info.value.code) == (422, "raise_implement_needs_sandbox_full")
    assert ctx.control.egress_mode(ctx.config.egress.mode_default) == "open"
    assert _audits(ctx, "egress_mode_changed") == []


def test_full_needs_the_mode_in_the_snapshot(ctx: AppContext) -> None:
    egress = ctx.config.egress
    old = egress.model_copy(update={"modes": {m: g for m, g in egress.modes.items() if m != "sandbox_full"}})
    with pytest.raises(EgressModeRefused) as info:
        ctx.control.set_egress_mode("sandbox_full", ACTOR, egress.mode_default, egress=old, raise_implement=True)
    assert (info.value.status, info.value.code) == (409, "mode_not_in_snapshot")
    assert ctx.control.egress_mode(egress.mode_default) == "open" and ctx.control.implement_network() == "off"


def test_switching_to_full_invalidates_nothing(ctx: AppContext) -> None:
    """§9.4: approvals bind the mirror, the snapshot and dry-run; the mode and the setting are neither."""
    install_sources(ctx)
    skill_card(ctx)
    proposal_card(ctx, proposed_contribution(ctx))
    cid, action = packaged_contribution(ctx)
    ctx.create_approval(action, "system")
    ctx.conn.execute("INSERT INTO contribution_holds (id, cid, reason, detail_json, held_by, held_at)"
                     " VALUES ('h_1', ?, 'egress_anomaly', '{}', 'sandbox', 't')", (cid,))  # fmt: skip
    ctx.conn.commit()
    before = _rows(ctx)
    _switch(ctx, "sandbox_full", raise_implement=True)
    _switch(ctx, "sandbox_open")
    assert _rows(ctx) == before and ctx.control.view().dry_run is True


async def test_api_raise_implement(ctx: AppContext) -> None:
    async with client(ctx, UID_WORKER) as c:
        assert (await c.post("/v1/control/egress-mode", json={"mode": "sandbox_full", "raise_implement": True})
                ).status_code == 403  # fmt: skip
    async with client(ctx, UID_OPERATOR) as c:
        bad = await c.post("/v1/control/egress-mode", json={"mode": "sandbox_open", "raise_implement": True})
        assert (bad.status_code, bad.json()["error"]) == (422, "raise_implement_needs_sandbox_full")
        assert (await c.post("/v1/control/egress-mode", json={"mode": "open", "extra": 1})).status_code == 400
        resp = await c.post("/v1/control/egress-mode", json={"mode": "sandbox_full", "raise_implement": True})
    assert resp.status_code == 200
    assert resp.json() == {"mode": "sandbox_full", "default": "open", "previous": "open",
                           "implement_network_previous": "off", "implement_network_error": None}  # fmt: skip


async def test_api_full_not_in_snapshot_is_409(ctx: AppContext) -> None:
    egress = ctx.config.egress
    old = egress.model_copy(update={"modes": {m: g for m, g in egress.modes.items() if m != "sandbox_full"}})
    snap = ctx.snapshots.current  # an old snapshot (pre-ADR-0025): no modes.sandbox_full
    ctx.snapshots._current = dataclasses.replace(snap, bundle=dataclasses.replace(snap.bundle, egress=old))
    async with client(ctx, UID_OPERATOR) as c:
        resp = await c.post("/v1/control/egress-mode", json={"mode": "sandbox_full", "raise_implement": True})
    assert (resp.status_code, resp.json()["error"]) == (409, "mode_not_in_snapshot")
    assert ctx.control.egress_mode(egress.mode_default) == "open"


async def test_sandbox_health_report_is_sandbox_only_and_kept_in_memory(ctx: AppContext) -> None:
    from ossbot.gatekeeper import egress_status
    from tests.conftest import UID_SANDBOX

    assert egress_status.floor_detector(ctx) == "down"  # no report yet
    async with client(ctx, UID_OPERATOR) as c:
        assert (await c.post("/v1/egress/health", json={"floor_detector": "ok"})).status_code == 403
    async with client(ctx, UID_SANDBOX) as c:
        assert (await c.post("/v1/egress/health", json={"floor_detector": "maybe"})).status_code == 400
        assert (await c.post("/v1/egress/health", json={"floor_detector": "ok", "x": 1})).status_code == 400
        assert (await c.post("/v1/egress/health", json={"floor_detector": "ok"})).json() == {"ok": True}
    assert egress_status.floor_detector(ctx) == "ok"
    assert ctx.conn.execute("SELECT COUNT(*) FROM audit_events WHERE event LIKE '%health%'").fetchone()[0] == 0


async def test_api_partial_failure_answers_200_with_the_error(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any) -> str:
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(ctx.control, "_write_implement_network", broken)
    async with client(ctx, UID_OPERATOR) as c:
        resp = await c.post("/v1/control/egress-mode", json={"mode": "sandbox_full", "raise_implement": True})
    doc = resp.json()
    assert resp.status_code == 200 and doc["mode"] == "sandbox_full" and doc["implement_network_previous"] is None
    assert "disk I/O error" in doc["implement_network_error"]
