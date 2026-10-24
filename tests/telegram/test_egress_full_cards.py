"""ADR-0025 cards (build-env design §5.2): the floor hold card per class (classic and rich goldens; the public
address is never shown, untrusted targets redacted and escaped, [▶️ Resume…] [📜 Logs]) and the digest's
"🌐 Egress notes" block (absent without notes).

Owner controls (build-env design §9.2, AC 10): the five-mode ``/egress`` picker, the ``sandbox_full`` confirm
panel (every implement-network value, both public-floor states), the open↔full panels and the ``/status``
egress line (public floor ok and unknown); classic and rich goldens."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest

from ossbot.common.clock import FakeClock
from ossbot.common.public_floor import FloorEntry, PublicFloorState
from ossbot.common.schemas import EgressNoteRequest, HoldRequest, SandboxHealthReport, State
from ossbot.gatekeeper import egress_status
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.egress_notes import EgressNotes
from ossbot.gatekeeper.telegram import digest, hold_cards
from ossbot.gatekeeper.telegram.commands import admin
from ossbot.gatekeeper.telegram.runtime import ui_of
from ossbot.gatekeeper.telegram.ui import blocks, classic, rich
from ossbot.gatekeeper.telegram.ui import kit as k
from tests.conftest import insert_contribution
from tests.fakes.holds import floor_request, note_body
from tests.telegram import golden
from tests.telegram.reviewkit import labels, latest, sent_with, text, token
from tests.telegram.tgharness import TgRig, make_rig

SANDBOX = "uid:2002/sandbox"
GHP = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
CASES = {
    "dst": ("repo.maven.apache.org", "10.0.0.1"),
    "name": ("uploads.github.com", "-"),
    "code": ("api.telegram.org", "-"),
    "literal": ("169.254.169.254", "169.254.169.254"),
    "host": ("172.21.192.1", "172.21.192.1"),
    "proxy": ("192.168.1.1", "192.168.1.1"),
    "pub": ("public-address", "-"),
}


def _floor_hold(ctx: AppContext, cls: str, target: str, dst: str, stage: str = "validate") -> tuple[str, k.View]:
    cid = insert_contribution(ctx, State.VALIDATING, upstream="acme/widgets", issue_number=123)
    ctx.holds.publish_hold = lambda v: None
    run_id = "impl-abc123" if stage == "implement" else "validate-abc123"
    found, _ = ctx.holds.hold(cid, floor_request(cls, target, dst, stage=stage, run_id=run_id), SANDBOX)
    return cid, hold_cards.hold_view(ctx, found)


@pytest.mark.parametrize("cls", sorted(CASES))
def test_floor_card_per_class_classic_and_rich(ctx: AppContext, cls: str) -> None:
    cid, view = _floor_hold(ctx, cls, *CASES[cls])
    keyboard = "\n".join(" | ".join(row) for row in view.keyboard_labels())
    shown = classic.render(view).text
    golden.check(f"floor_card_{cls}.html", shown.replace(cid, "c_CID") + "\n--\n" + keyboard + "\n")
    doc = blocks.dump(rich.render(replace(view, rich=True)))
    dumped = json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True)
    golden.check(f"floor_card_{cls}.json", dumped.replace(cid, "c_CID") + "\n")
    assert "Floor reach attempt" in shown and "Blocked; only this contribution is held." in shown
    assert hold_cards.FLOOR_CLASS_TEXT[cls] in shown.replace("&#x27;", "'")
    assert keyboard == "▶️ Resume… | 📜 Logs"


def test_the_public_address_is_never_shown(ctx: AppContext) -> None:
    _cid, view = _floor_hold(ctx, "pub", "public-address", "-")
    shown = classic.render(view).text
    assert "your public address" in shown and "public-address" not in shown
    rich_doc = json.dumps(blocks.dump(rich.render(replace(view, rich=True))), ensure_ascii=False)
    assert "public-address" not in rich_doc


def test_a_hostile_target_is_redacted_and_escaped(ctx: AppContext) -> None:
    """The schema already refuses such a target; the card redacts and escapes anyway (defence in depth)."""
    cid = insert_contribution(ctx, State.VALIDATING, upstream="acme/widgets", issue_number=123)
    ctx.holds.publish_hold = lambda v: None
    found, _ = ctx.holds.hold(cid, floor_request(), SANDBOX)
    body = found.detail.root
    hostile = body.model_copy(update={"floor": body.floor.model_copy(update={"target": f"{GHP}.<b>evil</b>"})})
    shown = classic.render(hold_cards.hold_view(ctx, found.model_copy(update={"detail": HoldRequest(hostile)}))).text
    assert GHP not in shown and "[REDACTED]" in shown and "<b>evil" not in shown


def test_implement_floor_card_names_the_executor(ctx: AppContext) -> None:
    _cid, view = _floor_hold(ctx, "code", "api.telegram.org", "-", stage="implement")
    assert "coder's executor (implement) job stopped" in classic.render(view).text


def _note(ctx: AppContext, cid: str, **overrides: object) -> None:
    EgressNotes(ctx.conn, ctx.clock, ctx.audit).record(cid, EgressNoteRequest.model_validate(note_body(**overrides)),
                                                       SANDBOX)  # fmt: skip


def test_digest_notes_block_classic_and_rich(ctx: AppContext, clock: FakeClock) -> None:
    cid = insert_contribution(ctx, State.VALIDATING, upstream="acme/widgets", issue_number=123)
    other = insert_contribution(ctx, State.VALIDATING, upstream="acme/<b>tools</b>", issue_number=7)
    _note(ctx, cid)
    _note(ctx, other, run_id="fetch-note02", stage="fetch", kind="floor_note", breached=[],
          notes=["loopback: lvh.me", "quiet: checkpoint-api.hashicorp.com"])  # fmt: skip
    clock.advance(digest.timedelta(minutes=1))
    period = digest.now_period(ctx, "daily")
    stats = digest.collect(ctx, period, {})
    assert stats["egress_notes"]["count"] == 2
    view = digest.view(ctx, period, stats, {})
    assert "egress_notes" in [b.name for b in view.blocks]
    block = digest.egress_notes_block(stats["egress_notes"])
    assert block is not None
    one = k.View(kind="digest", blocks=[block])
    shown = classic.render(one).text
    golden.check("digest_egress_notes.html", shown + "\n")
    golden.check("digest_egress_notes.json",
                 json.dumps(blocks.dump(rich.render(replace(one, rich=True))), ensure_ascii=False, indent=1,
                            sort_keys=True) + "\n")  # fmt: skip
    plain = shown.replace("\u2060", "")  # the redactor breaks auto-links with a word joiner
    assert "acme/widgets#123 · validate · 74 hosts (&gt; 60) · ↓ 7.2 GB (&gt; 6.0 GB)" in plain
    assert "<b>tools</b>" not in shown and "2 floor notes" in shown


def test_digest_without_notes_has_no_block(ctx: AppContext, clock: FakeClock) -> None:
    clock.advance(digest.timedelta(minutes=1))
    period = digest.now_period(ctx, "daily")
    stats = digest.collect(ctx, period, {})
    assert stats["egress_notes"] == {"count": 0, "items": []}
    assert "egress_notes" not in [b.name for b in digest.view(ctx, period, stats, {}).blocks]
    del stats["egress_notes"]  # an older stats_json row renders without it
    assert "egress_notes" not in [b.name for b in digest.view(ctx, period, stats, {}).blocks]


# ------------------------------------------- owner controls (build-env design §9.2, AC 10)
ACTOR = "tg:4242"
PANEL = [
    "Repo code (fetch, validate and the coder's executor) gets the full public web on ports 80 and 443, also for"
    " tools that ignore proxies. Other ports, UDP and URLs with a bare IP address stay closed.",
    "Volume and host limits no longer hold a contribution; you get a quiet note in the daily digest.",
    "Always blocked, and any attempt holds that contribution and alerts you: your Windows host, your LAN and other"
    " private ranges, your router's and this laptop's public address, cloud metadata, the distro's own services,"
    " Telegram, Kiro and GitHub upload hosts.",
    "Strict repos and the Kiro key containers are unchanged. Gate 2 is unchanged. Approvals stay valid.",
]


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def _floor(ctx: AppContext, known: bool) -> None:
    """A public-floor state.json in the test tree: known (answered now) or unknown since 100 s ago."""
    now = ctx.clock.now().timestamp()
    last = now if known else now - 400
    state = PublicFloorState(known=known, last_v4_success=last, generation=3,
                             v4=[FloorEntry(ip="9.9.9.9", first_seen=last, last_seen=last)])  # fmt: skip
    ctx.paths.floor_dir.mkdir(parents=True, exist_ok=True)
    (ctx.paths.floor_dir / "state.json").write_text(state.to_json(), encoding="utf-8")


def _mode(ctx: AppContext) -> str:
    return ctx.control.egress_mode(ctx.config.egress.mode_default)


def _switch(ctx: AppContext, mode: str) -> None:
    egress = ctx.config.egress
    ctx.control.set_egress_mode(mode, ACTOR, egress.mode_default, egress=egress)  # type: ignore[arg-type]


def _golden_view(name: str, view: k.View) -> str:
    keyboard = "\n".join(" | ".join(row) for row in view.keyboard_labels())
    body = classic.render(view).text
    golden.check(f"{name}.html", body + "\n--\n" + keyboard + "\n")
    doc = blocks.dump(rich.render(replace(view, rich=True)))
    golden.check(f"{name}.json", json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    assert "9.9.9.9" not in body and "9.9.9.9" not in json.dumps(doc)  # the address is never shown
    return body


@pytest.mark.parametrize("known", [True, False], ids=["floor_known", "floor_unknown"])
def test_picker_offers_five_modes(ctx: AppContext, known: bool) -> None:
    _floor(ctx, known)
    view = admin.egress_picker(ctx)
    body = _golden_view(f"egress_full_picker_{'known' if known else 'unknown'}", view)
    assert [lbl.split()[0] for row in view.keyboard_labels() for lbl in row][:5] == list(admin.TELEGRAM_EGRESS_MODES)
    assert ("Public floor: ok" if known else "Public floor: unknown since") in body


@pytest.mark.parametrize("impl", ["off", "on_request", "auto"])
@pytest.mark.parametrize("known", [True, False], ids=["floor_known", "floor_unknown"])
def test_full_confirm_panel(ctx: AppContext, impl: str, known: bool) -> None:
    _floor(ctx, known)
    ctx.control.set_implement_network(impl, ACTOR, ctx.config.egress.mode_default)  # type: ignore[arg-type]
    view = admin.egress_confirm_view(ctx, "sandbox_full")
    body = _golden_view(f"egress_full_confirm_{impl}_{'known' if known else 'unknown'}", view)
    assert body.splitlines()[0] == "🌐 Egress mode open → sandbox_full. Root re-renders the proxy."
    for line in PANEL:
        assert line in body
    raise_line = f"Implement network {impl} → auto (the coder's executor gets the web too)."
    assert (raise_line in body) is (impl != "auto")
    state = "known" if known else "not known yet"
    assert (f"Your public address is checked every minute (now: {state}); while it can't be checked, repo code's"
            " web pauses and its jobs wait.") in body  # fmt: skip
    confirm, _cancel = view.rows[0]
    assert (confirm.label, confirm.style, confirm.single_use) == ("✅ Switch to sandbox_full", "danger", True)


def test_open_to_full_and_back_panels(ctx: AppContext) -> None:
    _floor(ctx, True)
    _switch(ctx, "sandbox_open")
    body = _golden_view("egress_full_confirm_from_sandbox_open", admin.egress_confirm_view(ctx, "sandbox_full"))
    assert "Fetch and validate keep the public web; volume limits hold → are notes." in body
    _switch(ctx, "sandbox_full")
    view = admin.egress_confirm_view(ctx, "sandbox_open")
    body = _golden_view("egress_full_to_sandbox_open", view)
    assert "Fetch and validate keep the public web; volume limits notes → hold again." in body
    assert view.rows[0][0].style == "primary"  # narrowing


@pytest.mark.parametrize("known", [True, False], ids=["floor_known", "floor_unknown"])
def test_status_egress_line(ctx: AppContext, known: bool) -> None:
    _floor(ctx, known)
    _switch(ctx, "sandbox_full")
    ctx.control.set_implement_network("auto", ACTOR, ctx.config.egress.mode_default)
    if known:
        egress_status.record(ctx, SandboxHealthReport(floor_detector="ok"))
    view = ui_of(ctx).dashboard.view(None, kind="status")
    body = classic.render(view).text
    line = next(ln for ln in body.splitlines() if ln.startswith("🌐 Egress"))
    golden.check(f"status_egress_line_{'known' if known else 'unknown'}.html", line + "\n")
    doc = blocks.dump(rich.render(replace(view, rich=True)))
    golden.check(f"status_egress_line_{'known' if known else 'unknown'}.json",
                 json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n")  # fmt: skip
    assert line in classic.render(view).text and "Egress sandbox_full" in json.dumps(doc, ensure_ascii=False)
    if known:
        assert line == ("🌐 Egress sandbox_full · implement network auto (active) · floor detector ok · public"
                        " floor ok")  # fmt: skip
    else:
        since = egress_status.since_text(egress_status.public_floor(ctx))
        assert line == ("🌐 Egress sandbox_full · implement network auto (inactive, public floor unknown) · floor"
                        f" detector down · public floor unknown since {since} UTC, repo-code web paused")  # fmt: skip


def test_a_stale_detector_report_reads_down(ctx: AppContext) -> None:
    egress_status.record(ctx, SandboxHealthReport(floor_detector="ok"))
    assert egress_status.floor_detector(ctx) == "ok"
    ctx.clock.advance(seconds=egress_status.HEALTH_MAX_AGE_S + 1)  # type: ignore[attr-defined]
    assert egress_status.floor_detector(ctx) == "down"


async def test_confirm_switches_and_raises_implement_network(rig: TgRig) -> None:
    ctx = rig.ctx
    _floor(ctx, True)
    await rig.say("/egress sandbox_full")
    mid = sent_with(rig, "Egress mode open → sandbox_full")
    assert _mode(ctx) == "open" and ctx.control.implement_network() == "off"  # nothing before the confirm
    assert rig.buttons(latest(rig, mid))[0][0]["style"] == "danger"
    await rig.tap(token(rig, mid, "✅ Switch to sandbox_full"), message=mid)
    assert _mode(ctx) == "sandbox_full" and ctx.control.implement_network() == "auto"
    events = [r[0] for r in ctx.conn.execute(
        "SELECT event FROM audit_events WHERE event IN ('egress_mode_changed', 'implement_network_changed')"
        " ORDER BY seq")]  # fmt: skip
    assert events == ["egress_mode_changed", "implement_network_changed"]
    fyis = [json.loads(r[0]).get("text", "") for r in ctx.conn.execute("SELECT payload_json FROM tg_outbox")]
    assert any("Egress mode open → sandbox_full by" in t and "implement network off → auto" in t for t in fyis)


async def test_picker_tap_opens_the_full_panel(rig: TgRig) -> None:
    await rig.say("/egress")
    mid = sent_with(rig, "Egress mode")
    assert "sandbox_full" in labels(rig, mid)
    await rig.tap(token(rig, mid, "sandbox_full"), message=mid)
    assert "Volume and host limits no longer hold a contribution" in text(rig, mid)


async def test_partial_failure_reply(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    from ossbot.gatekeeper.telegram.commands.registry import CommandContext

    def broken(*args: object) -> str:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(ctx.control, "_write_implement_network", broken)
    reply = await admin.egress_confirmed(CommandContext(app=ctx, tg_user_id=4242), "sandbox_full")
    assert reply.text.startswith("🌐 Egress mode switched; implement network unchanged (off): RuntimeError: database")
    assert _mode(ctx) == "sandbox_full"


async def test_full_not_in_snapshot_is_refused(ctx: AppContext) -> None:
    from dataclasses import replace as dc_replace

    from ossbot.gatekeeper.telegram.commands.registry import CommandContext

    egress = ctx.config.egress
    old = egress.model_copy(update={"modes": {m: g for m, g in egress.modes.items() if m != "sandbox_full"}})
    snap = ctx.snapshots.current
    ctx.snapshots._current = dc_replace(snap, bundle=dc_replace(snap.bundle, egress=old))
    reply = await admin.egress_confirmed(CommandContext(app=ctx, tg_user_id=4242), "sandbox_full")
    assert reply.text.startswith("Not switched: the running config snapshot has no modes.sandbox_full")
    assert _mode(ctx) == "open"
