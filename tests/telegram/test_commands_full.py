"""The §14.14.1 command set (DESIGN §14.14.1-2, §14.10.2, §14.13.2; DES-FR-6, TGRW-14): every M1 command is
registered with a short verb-first menu description, confirms carry ``[✅ <verb>] [↩️ Cancel]``, and the
personal and read commands (/quiet /notify /settings /new /limits /logs /repos /reload /skills) work."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest

from ossbot.common.clock import to_iso
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.commands.personal import MIRROR_INFO
from ossbot.gatekeeper.telegram.commands.registry import COMMANDS, MENU_DESCRIPTION_MAX, menu
from ossbot.gatekeeper.telegram.runtime import ui_of
from tests.conftest import insert_contribution
from tests.telegram.reviewkit import labels, sent_with, text, toast, token
from tests.telegram.tgharness import TgRig, make_rig

M1 = {"start", "help", "settings", "status", "dashboard", "queue", "work", "size", "filters", "approve", "review",
      "pause", "resume", "kill", "dryrun", "drain", "undrain", "down", "up", "reload", "egress", "hermes", "model",
      "repos", "limits", "budget", "logs", "skills", "new", "newchat", "quiet", "notify"}  # fmt: skip


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def test_every_m1_command_is_registered_with_a_short_description() -> None:
    assert set(COMMANDS) >= M1, M1 - set(COMMANDS)
    for state in ("running", "paused", "killed", "draining", "down"):
        for item in menu(state):  # type: ignore[arg-type]
            assert 0 < len(item["description"]) <= MENU_DESCRIPTION_MAX
    assert [c["command"] for c in menu("killed")] == ["status", "resume", "logs", "help", "start"]
    assert "up" in {c["command"] for c in menu("down")} and "up" not in {c["command"] for c in menu("running")}
    assert "undrain" in {c["command"] for c in menu("draining")}


async def test_go_confirms_carry_cancel(rig: TgRig) -> None:
    rig.ctx.control.pause("test", "tg:4242")
    await rig.say("/resume")
    mid = sent_with(rig, "Resume from")
    assert labels(rig, mid) == ["▶️ Confirm resume", "↩️ Cancel"]
    await rig.tap(token(rig, mid, "↩️ Cancel"), message=mid)
    assert rig.ctx.control.view().state == "paused" and not rig.ctx.paths.resume_request.exists()


async def test_quiet_picker_holds_p2_and_off_releases(rig: TgRig) -> None:
    await rig.say("/quiet")
    mid = sent_with(rig, "Not quiet")
    assert labels(rig, mid) == ["1 h", "3 h", "Until 08:00", "Until tomorrow", "Off"]
    await rig.tap(token(rig, mid, "3 h"), message=mid)
    notifier = ui_of(rig.ctx).notifier
    until = notifier.quiet_until()
    assert until is not None and until - rig.ctx.clock.now() == timedelta(hours=3)
    assert "Quiet until" in text(rig, mid)
    rig.ctx.notify("📈 discovery found 2")
    assert db.one(rig.ctx.conn, "SELECT COUNT(*) FROM tg_outbox WHERE status = 'held'")[0] == 1
    await rig.tap(token(rig, mid, "Off"), message=mid)
    assert notifier.quiet_until() is None


async def test_notify_toggle_mutes_never_drops(rig: TgRig) -> None:
    await rig.say("/notify")
    mid = sent_with(rig, "Notifications")
    assert "✅ Approvals" in labels(rig, mid) and len(labels(rig, mid)) == 9
    await rig.tap(token(rig, mid, "✅ Approvals"), message=mid)
    assert "⬜ Approvals" in labels(rig, mid)
    assert ui_of(rig.ctx).notifier.toggles()["approvals"] is False


async def test_settings_buttons(rig: TgRig) -> None:
    await rig.say("/settings")
    mid = sent_with(rig, "Settings")
    assert labels(rig, mid) == ["🖼 Rich views: auto", "🔕 Notifications…", "🌙 Quiet hours…", "📰 Digest…",
                                "🐙 Mirror: off ⓘ"]  # fmt: skip
    await rig.tap(token(rig, mid, "🐙 Mirror"), message=mid)
    assert toast(rig) == MIRROR_INFO
    await rig.tap(token(rig, mid, "🖼 Rich views"), message=mid)
    assert labels(rig, mid)[0] == "🖼 Rich views: on"
    await rig.tap(token(rig, mid, "🔕 Notifications"), message=mid)
    assert "✅ Approvals" in labels(rig, mid)


async def test_new_starts_a_fresh_session(rig: TgRig) -> None:
    turns = rig.ctx.chat.service.turns  # type: ignore[union-attr]
    before = turns.session_key(4242)
    await rig.say("/new")
    assert turns.session_key(4242) != before and "New Hermes conversation" in rig.last_text()
    await rig.say("/newchat")
    assert "New Hermes conversation" in rig.last_text()


async def test_limits_and_budget(rig: TgRig) -> None:
    await rig.say("/limits")
    out = rig.last_text()
    assert "Usage vs limits" in out and "PRs: 0/" in out and "Issue claims: 0/" in out
    await rig.say("/budget")
    assert rig.last_text() == out


async def test_logs_reads_only_the_logs_dir_and_redacts(rig: TgRig) -> None:
    ctx = rig.ctx
    cid = insert_contribution(ctx)
    log = ctx.paths.logs_dir / "fetch.log"
    secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    log.write_text("line one\nleaked " + secret + "\nlast line\n", encoding="utf-8")
    outside = ctx.paths.gatekeeper_dir / "secret.log"
    outside.write_text("not for telegram\n", encoding="utf-8")
    now = to_iso(ctx.clock.now())
    with db.transaction(ctx.conn):
        for rid, stage, path in (("sr_1", "fetch", str(log)), ("sr_2", "vet", str(outside))):
            ctx.conn.execute("INSERT INTO stage_runs (id, cid, stage, attempt, status, started_at, log_path,"
                             " error_message) VALUES (?, ?, ?, 1, 'failed', ?, ?, 'boom')",
                             (rid, cid, stage, now, path))  # fmt: skip
    await rig.say("/logs")
    mid = sent_with(rig, "which contribution")
    await rig.tap(token(rig, mid, cid), message=mid)
    assert any(lbl.startswith("fetch #1") for lbl in labels(rig, mid))
    await rig.tap(token(rig, mid, "fetch #1"), message=mid)
    payloads = " ".join(r[0] for r in ctx.conn.execute("SELECT payload_json FROM tg_outbox"))
    assert "last line" in payloads and secret not in payloads
    assert [m for m, _ in rig.req.calls if m == "sendDocument"]
    await rig.say(f"/logs {cid} vet")
    assert "not for telegram" not in rig.last_text() and "boom" in rig.last_text()


async def test_repos_disable_needs_a_confirm(rig: TgRig) -> None:
    await rig.say("/repos")
    mid = sent_with(rig, "Target repos")
    first = labels(rig, mid)[0]
    await rig.tap(token(rig, mid, first), message=mid)
    repo = text(rig, mid).split("<b>", 1)[1].split("</b>", 1)[0]
    await rig.tap(token(rig, mid, "⏸ Disable"), message=mid)
    assert labels(rig, mid) == ["✅ Confirm", "↩️ Cancel"]
    assert db.one(rig.ctx.conn, "SELECT 1 FROM repo_overrides WHERE upstream = ?", (repo,)) is None
    await rig.tap(token(rig, mid, "✅ Confirm"), message=mid)
    row = db.one(rig.ctx.conn, "SELECT enabled FROM repo_overrides WHERE upstream = ?", (repo,))
    assert row is not None and row["enabled"] == 0


async def test_repos_undoes_never_this_repo(rig: TgRig) -> None:
    from ossbot.gatekeeper.telegram.commands.admin import all_targets

    repo = all_targets(rig.ctx)[0]
    rig.ctx.discovery_prefs.never_repo(repo, "tg:4242")
    await rig.say("/repos")
    mid = sent_with(rig, "Target repos")
    await rig.tap(token(rig, mid, next(lbl for lbl in labels(rig, mid) if "🚫" in lbl)), message=mid)
    await rig.tap(token(rig, mid, "↩️ Propose again"), message=mid)
    assert rig.ctx.discovery_prefs.effective(repo).never_repo is False


async def test_reload_shows_the_changes_then_confirms(rig: TgRig) -> None:
    await rig.say("/reload")
    assert "Nothing to reload" in rig.last_text()
    policy = rig.ctx.paths.config_dir / "policy.yaml"
    policy.write_text(policy.read_text(encoding="utf-8") + "\n# telegram reload test\n", encoding="utf-8")
    await rig.say("/reload")
    mid = sent_with(rig, "Reload")
    assert "policy.yaml" in text(rig, mid) and labels(rig, mid) == ["✅ Reload", "↩️ Cancel"]
    before = rig.ctx.snapshots.current.hash
    await rig.tap(token(rig, mid, "✅ Reload"), message=mid)
    assert rig.ctx.snapshots.current.hash != before and "Reloaded" in text(rig, mid)


async def test_review_without_open_cards(rig: TgRig) -> None:
    await rig.say("/review")
    assert "No open PR cards to review" in rig.last_text()
    await rig.say("/review nope")
    assert "Usage: /review" in rig.last_text()


async def test_review_opens_the_explorer_of_an_open_card(rig: TgRig) -> None:
    from tests.conftest import packaged_contribution

    cid, action = packaged_contribution(rig.ctx)
    approval = rig.ctx.create_approval(action, "test")
    rig.ctx.on_approval_created(approval)
    await rig.flush()
    await rig.say("/review")
    mid = sent_with(rig, "Review which card?")
    await rig.tap(token(rig, mid, f"🧭 {cid}"), message=mid)
    assert sent_with(rig, f"🧭 <b>{cid} r")


async def test_skills_lists_nothing_yet(rig: TgRig) -> None:
    await rig.say("/skills")
    assert "No skills yet" in json.dumps(rig.req.calls[-1][1], ensure_ascii=False)
