"""ADR-0025 cards (build-env design §5.2): the floor hold card per class (classic and rich goldens; the public
address is never shown, untrusted targets redacted and escaped, [▶️ Resume…] [📜 Logs]) and the digest's
"🌐 Egress notes" block (absent without notes)."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from ossbot.common.clock import FakeClock
from ossbot.common.schemas import EgressNoteRequest, HoldRequest, State
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.egress_notes import EgressNotes
from ossbot.gatekeeper.telegram import digest, hold_cards
from ossbot.gatekeeper.telegram.ui import blocks, classic, rich
from ossbot.gatekeeper.telegram.ui import kit as k
from tests.conftest import insert_contribution
from tests.fakes.holds import floor_request, note_body
from tests.telegram import golden

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
