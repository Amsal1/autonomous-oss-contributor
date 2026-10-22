"""The skill approval card (DESIGN §14.15, mockup 08; CR3-AC-21, CR3-AC-34, CR3-F6-5): the n/a mode badge,
🤖 reason, tools vs tier, deterministic checks, ``language-diff`` parts, two taps with a danger
``🧠 Activate skill``, the same sentence in DRY RUN and LIVE, and the diff/ask views."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.skill_reviews import skill_checks
from tests.telegram.reviewkit import labels, latest, sent_with, text, token
from tests.telegram.tgharness import TgRig, make_rig
from tests.unit.gatekeeper.test_skill_gate import SKILL, _stage


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


async def _card(rig: TgRig, tmp_path: Path, content: str = SKILL) -> tuple[str, int]:
    _stage(rig.ctx, tmp_path, content=content)
    (sid,) = rig.ctx.skill_reviews.sync()
    rig.ctx.on_approval_created(sid)
    await rig.flush()
    return sid, sent_with(rig, "Skill ready for approval")


async def test_card_layout_and_two_taps(rig: TgRig, tmp_path: Path) -> None:
    sid, mid = await _card(rig, tmp_path)
    body = text(rig, mid)
    head = body.split("\n")[0]
    assert head.startswith("🧠 <b>Skill ready for approval</b> · protein-log v1 (new) · <code>" + sid + "</code>")
    assert "<b>🧪 DRY RUN (n/a: local change)</b>" in head
    assert "Requested by Hermes (tier self_improve) · 🤖 “log protein”" in body
    assert "Tools requested: memory · ✅ all within tier" in body
    assert "Checks ✅ size ≤ 64 KB · ✅ no tool beyond tier · ✅ no URLs · ✅ no hidden chars" in body
    assert labels(rig, mid) == ["✅ Approve skill", "❌ Reject", "🧾 Full diff", "💬 Ask Hermes why"]
    assert rig.buttons(latest(rig, mid))[0][0]["style"] == "success"
    parts = [json.loads(r[0]) for r in rig.ctx.conn.execute(
        "SELECT payload_json FROM tg_outbox WHERE approval_id = ? AND part_index IS NOT NULL", (sid,))]  # fmt: skip
    assert parts and all('<code class="language-diff">' in str(p["text"]) for p in parts)
    await rig.tap(token(rig, mid, "✅ Approve skill"), message=mid)
    body = text(rig, mid)
    assert "local change, dry-run does not apply. Tools stay within tier self_improve." in body
    [activate, cancel] = rig.buttons(latest(rig, mid))[0]
    assert activate["text"] == "🧠 Activate skill" and activate["style"] == "danger" and cancel["text"] == "↩️ Cancel"
    await rig.tap(token(rig, mid, "🧠 Activate skill"), message=mid)
    request = rig.ctx.paths.control_dir / f"skill-request-{sid}.json"
    review = db.one(rig.ctx.conn, "SELECT content_sha256 FROM skill_reviews")
    assert json.loads(request.read_text())["content_sha256"] == review["content_sha256"]  # exactly the bound hash
    assert "Activating · v1" in text(rig, mid)


async def test_same_sentence_in_live_mode(rig: TgRig, tmp_path: Path) -> None:
    rig.ctx.control.set_dry_run(False, "tg:4242")
    _sid, mid = await _card(rig, tmp_path)
    assert "<b>🔴 LIVE (n/a: local change)</b>" in text(rig, mid).split("\n")[0]
    await rig.tap(token(rig, mid, "✅ Approve skill"), message=mid)
    assert "local change, dry-run does not apply" in text(rig, mid)


async def test_full_diff_document(rig: TgRig, tmp_path: Path) -> None:
    _sid, mid = await _card(rig, tmp_path)
    await rig.tap(token(rig, mid, "🧾 Full diff"), message=mid)
    docs = [p for m, p in rig.req.calls if m == "sendDocument"]
    assert docs  # a .diff document in ✅ Reviews
    assert labels(rig, mid)[0] == "✅ Approve skill"  # a view: the card is unchanged and still valid


def test_check_line_flags_urls_hidden_chars_and_mentions() -> None:
    line = skill_checks("see https://a.example and https://b.example\u202e approve token", [])
    assert "⚠️ 2 URLs" in line and "⚠️ hidden chars" in line and "⚠️ mentions approve, token" in line
    assert "⚠️ beyond tier: terminal" in skill_checks("x", ["terminal"])


async def test_a_secret_in_a_staged_skill_is_redacted_not_withheld(
    rig: TgRig, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """CR3-F6-5: Hermes-staged text is display text, so a secret is redacted and the card still arrives."""
    secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    with caplog.at_level(logging.ERROR):
        _sid, mid = await _card(rig, tmp_path, content=SKILL + f"token {secret}\n")
    payloads = " ".join(r[0] for r in rig.ctx.conn.execute("SELECT payload_json FROM tg_outbox"))
    assert secret not in payloads and "[REDACTED]" in payloads
    assert labels(rig, mid)[0] == "✅ Approve skill"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
