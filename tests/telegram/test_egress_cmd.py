"""``/egress`` (DESIGN §14.14.1, §11.5; CR2-2a): a picker, then a confirm panel listing the categories per
grantee that change; only the confirm tap writes the flag (audited, FYI). Widening is styled danger."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest

from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.commands import admin
from ossbot.gatekeeper.telegram.ui import blocks, classic, rich
from ossbot.gatekeeper.telegram.ui import kit as k
from tests.telegram import golden
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
    assert [lbl.split()[0] for lbl in labels(rig, mid)][:4] == ["allowlist", "research", "open", "sandbox_open"]
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


# ------------------------------------------------------------------ implement network (ADR-0024)
def _impl(ctx: AppContext) -> str:
    return ctx.control.implement_network()


def _golden_view(name: str, view: k.View) -> None:
    keyboard = "\n".join(" | ".join(row) for row in view.keyboard_labels())
    golden.check(f"{name}.html", classic.render(view).text + "\n--\n" + keyboard + "\n")
    doc = blocks.dump(rich.render(replace(view, rich=True)))
    golden.check(f"{name}.json", json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n")


def test_picker_golden_with_the_impl_row(ctx: AppContext) -> None:
    _golden_view("egress_picker", admin.egress_picker(ctx))
    default = ctx.config.egress.mode_default
    ctx.control.set_egress_mode("sandbox_open", "tg:4242", default, egress=ctx.config.egress)
    ctx.control.set_implement_network("on_request", "tg:4242", default)
    _golden_view("egress_picker_on_request_active", admin.egress_picker(ctx))


@pytest.mark.parametrize(("start", "mode", "value"), [("off", "open", "on_request"), ("on_request", "open", "auto"),
                                                      ("off", "sandbox_open", "auto"), ("auto", "open", "off"),
                                                      ("auto", "sandbox_open", "on_request")])  # fmt: skip
def test_confirm_panel_goldens(ctx: AppContext, start: str, mode: str, value: str) -> None:
    default = ctx.config.egress.mode_default
    if mode != default:
        ctx.control.set_egress_mode(mode, "tg:4242", default, egress=ctx.config.egress)  # type: ignore[arg-type]
    ctx.control.set_implement_network(start, "tg:4242", default)  # type: ignore[arg-type]
    view = admin.implnet_confirm_view(ctx, value)
    keyboard = "\n".join(" | ".join(row) for row in view.keyboard_labels())
    golden.check(f"implnet_confirm_{start}_{value}_{mode}.html", classic.render(view).text + "\n--\n" + keyboard + "\n")
    widening = ["off", "on_request", "auto"].index(value) > ["off", "on_request", "auto"].index(start)
    assert view.rows[0][0].style == ("danger" if widening else "primary")


async def test_impl_picker_then_confirm_sets_the_value_and_audits(rig: TgRig) -> None:
    ctx = rig.ctx
    await rig.say("/egress")
    mid = sent_with(rig, "Egress mode")
    assert "Implement network: off" in text(rig, mid)
    assert labels(rig, mid)[-3:] == ["impl: off ✅", "impl: on_request", "impl: auto"]
    await rig.tap(token(rig, mid, "impl: auto"), message=mid)
    body = text(rig, mid)
    assert "Implement network off → auto" in body and "(now: open, so inactive until you switch)" in body
    assert _impl(ctx) == "off"  # nothing before the confirm
    assert rig.buttons(latest(rig, mid))[0][0]["style"] == "danger"
    await rig.tap(token(rig, mid, "✅ Set auto"), message=mid)
    assert _impl(ctx) == "auto" and _mode(ctx) == "open"  # the egress mode is untouched
    rows = ctx.conn.execute("SELECT data_json FROM audit_events WHERE event = 'implement_network_changed'")
    assert [json.loads(r[0]) for r in rows] == [{"from": "off", "to": "auto", "egress_mode": "open"}]
    fyis = [json.loads(r[0]).get("text", "") for r in ctx.conn.execute("SELECT payload_json FROM tg_outbox")]
    assert any("Implement network off → auto" in t for t in fyis)


async def test_impl_shortcut_and_narrowing_is_primary(rig: TgRig) -> None:
    ctx = rig.ctx
    ctx.control.set_implement_network("auto", "tg:4242", ctx.config.egress.mode_default)
    await rig.say("/egress implement off")
    mid = sent_with(rig, "Implement network auto → off")
    assert "never gets a network" in text(rig, mid)
    assert rig.buttons(latest(rig, mid))[0][0]["style"] == "primary"
    await rig.tap(token(rig, mid, "↩️ Cancel"), message=mid)
    assert _impl(ctx) == "auto"
    await rig.say("/egress implement auto")  # already auto: the picker instead
    assert sent_with(rig, "Implement network: auto (inactive)")


async def test_an_invalid_impl_confirm_is_refused(ctx: AppContext) -> None:
    from ossbot.gatekeeper.telegram.commands.registry import CommandContext

    reply = await admin.implnet_confirmed(CommandContext(app=ctx, tg_user_id=4242), "everything")
    assert reply.text == "This button is no longer valid." and _impl(ctx) == "off"


async def test_tapping_the_current_mode_changes_nothing(rig: TgRig) -> None:
    start = _mode(rig.ctx)
    await rig.say("/egress")
    mid = sent_with(rig, "Egress mode")
    await rig.tap(token(rig, mid, f"{start} ✅"), message=mid)
    assert rig.req.last("answerCallbackQuery")["text"] == f"Already {start}."
