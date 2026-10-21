"""``/egress`` (DESIGN §14.14.1, §11.5; CR2-2a): a picker, then a confirm panel listing the categories per
grantee that change; only the confirm tap writes the flag (audited, FYI). Widening is styled danger."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.commands import admin
from tests.telegram.reviewkit import labels, latest, sent_with, text, token
from tests.telegram.tgharness import TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def _mode(ctx: AppContext) -> str:
    return ctx.control.egress_mode(ctx.config.egress.mode_default)


async def test_picker_then_confirm_switches_the_mode(rig: TgRig) -> None:
    ctx = rig.ctx
    start = _mode(ctx)
    target = "allowlist" if start != "allowlist" else "research"
    await rig.say("/egress")
    mid = sent_with(rig, "Egress mode")
    assert [lbl.split()[0] for lbl in labels(rig, mid)] == ["allowlist", "research", "open", "sandbox_open"]
    assert "only sandbox_open also gives fetch and validate web_public" in text(rig, mid)
    assert f"{start} ✅" in labels(rig, mid)
    await rig.tap(token(rig, mid, target), message=mid)
    body = text(rig, mid)
    assert f"Egress mode {start} → {target}" in body
    assert _mode(ctx) == start  # nothing before the confirm
    for change in admin.egress_changes(ctx, start, target):
        assert change.split(":")[0] in body
    await rig.tap(token(rig, mid, f"✅ Switch to {target}"), message=mid)
    assert _mode(ctx) == target
    events = [r[0] for r in ctx.conn.execute("SELECT event FROM audit_events WHERE event = 'egress_mode_changed'")]
    assert events == ["egress_mode_changed"]


async def test_widening_is_danger_and_narrowing_primary(rig: TgRig) -> None:
    ctx = rig.ctx
    ctx.control.set_egress_mode("allowlist", "tg:4242", ctx.config.egress.mode_default, egress=ctx.config.egress)
    await rig.say("/egress open")
    mid = sent_with(rig, "Egress mode allowlist → open")
    [confirm, _cancel] = rig.buttons(latest(rig, mid))[0]
    assert confirm["style"] == "danger"
    await rig.tap(token(rig, mid, "↩️ Cancel"), message=mid)
    assert _mode(ctx) == "allowlist"
    ctx.control.set_egress_mode("open", "tg:4242", ctx.config.egress.mode_default, egress=ctx.config.egress)
    await rig.say("/egress allowlist")
    mid = sent_with(rig, "Egress mode open → allowlist")
    assert rig.buttons(latest(rig, mid))[0][0]["style"] == "primary"


async def test_sandbox_open_panel_names_repo_code_and_the_guard(rig: TgRig) -> None:
    """Design §12.2: open → sandbox_open lists fetch/validate +web_public, the guard line with the
    configured anomaly limits, and is styled danger; the way back names -web_public, styled primary."""
    ctx = rig.ctx
    assert _mode(ctx) == "open"
    await rig.say("/egress sandbox_open")
    mid = sent_with(rig, "Egress mode open → sandbox_open")
    body = text(rig, mid)
    assert "fetch: +web_public" in body and "validate: +web_public" in body
    a = ctx.config.egress.anomaly
    assert (
        "Repo code (fetch/validate) gets web_public, guarded by the per-job anomaly limits"
        f" (≤ {a.max_hosts} hosts, ↑ {a.max_upload_mb} MB, ↓ {a.max_download_mb} MB); strict repos keep the allowlist."
    ) in body
    assert "fetch and validate are unchanged" not in body.lower()
    assert rig.buttons(latest(rig, mid))[0][0]["style"] == "danger"
    await rig.tap(token(rig, mid, "✅ Switch to sandbox_open"), message=mid)
    assert _mode(ctx) == "sandbox_open"
    fyis = [json.loads(r[0]).get("text", "") for r in ctx.conn.execute("SELECT payload_json FROM tg_outbox")]
    assert any("Egress mode open → sandbox_open" in t and "fetch/validate +web_public" in t for t in fyis)
    await rig.say("/egress open")
    mid = sent_with(rig, "Egress mode sandbox_open → open")
    body = text(rig, mid)
    assert "fetch: −web_public" in body and "goes back to its package allowlist" in body
    assert rig.buttons(latest(rig, mid))[0][0]["style"] == "primary"


async def test_switches_that_leave_repo_code_alone_say_so(rig: TgRig) -> None:
    await rig.say("/egress research")
    mid = sent_with(rig, "Egress mode open → research")
    body = text(rig, mid)
    assert "Fetch and validate are unchanged." in body and "fetch:" not in body


async def test_tapping_the_current_mode_changes_nothing(rig: TgRig) -> None:
    start = _mode(rig.ctx)
    await rig.say("/egress")
    mid = sent_with(rig, "Egress mode")
    await rig.tap(token(rig, mid, f"{start} ✅"), message=mid)
    assert rig.req.last("answerCallbackQuery")["text"] == f"Already {start}."
