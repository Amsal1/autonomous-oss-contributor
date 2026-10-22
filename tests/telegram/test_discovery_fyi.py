"""Discovery visibility (discovery fix 5b, 5d): the per-cycle Discovery FYI and the 12 h stall warning.
Classic and rich goldens, created cids one tap to copy, subjects from the gatekeeper's own rows, untrusted
text redacted and escaped, no buttons, silent P2 held in quiet hours, delivered through the rig with the
copyable-id lint; the stall warning once per episode and never while killed, paused or draining."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ossbot.common.clock import FakeClock, to_iso
from ossbot.common.schemas import DiscoveryCycleReport, State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import discovery_fyi
from ossbot.gatekeeper.telegram.notify import Notifier
from ossbot.gatekeeper.telegram.ui import blocks, classic, rich
from ossbot.gatekeeper.telegram.ui import kit as k
from tests.conftest import insert_contribution
from tests.telegram import golden
from tests.telegram.reviewkit import labels, sent_with, text
from tests.telegram.tgharness import TgRig, make_rig

CID = "c_D1B6RE1ZRS"
GHP_LOWER = "ghp_" + "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8"  # token-shaped and a valid reason code
WORKER = "uid:2001/worker"


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def report(ctx: AppContext, **kw: Any) -> DiscoveryCycleReport:
    now = to_iso(ctx.clock.now())
    doc: dict[str, Any] = {"outcome": "finished", "scope": "full", "started_at": now, "ended_at": now,
                           "targets_read": 71, "kept_read": 1, "open_repos_scanned": 5, "candidates": 38,
                           "open_candidates": 6, "ranked": 12, "open_ranked": 2, "refused": 9, "linked_pr": 2,
                           "created_total": 0, "drops": {"stale_issue": 14, "too_big:m": 7, "labels": 3,
                                                         "language": 1}, **kw}  # fmt: skip
    return DiscoveryCycleReport.model_validate(doc)


def goldens(name: str, view: k.View, cids: tuple[str, ...] = ()) -> None:
    shown = classic.render(view).text
    dumped = json.dumps(blocks.dump(rich.render(replace(view, rich=True))), ensure_ascii=False, indent=1,
                        sort_keys=True)  # fmt: skip
    for cid in cids:
        shown, dumped = shown.replace(cid, "c_CID"), dumped.replace(cid, "c_CID")
    golden.check(f"{name}.html", shown + "\n")
    golden.check(f"{name}.json", dumped + "\n")


def created_row(ctx: AppContext, cid: str = CID, upstream: str = "runatlantis/atlantis", number: int = 2607) -> Any:
    insert_contribution(ctx, State.DISCOVERED, upstream=upstream, issue_number=number, id=cid)
    return discovery_fyi._rows(ctx, [cid])


# ------------------------------------------------------------------ the cycle FYI
def test_cycle_fyi_classic_and_rich(ctx: AppContext) -> None:
    rows = created_row(ctx)
    view = discovery_fyi.cycle_view(report(ctx, created=[CID], created_total=1), rows)
    goldens("discovery_cycle", view, (CID,))
    shown = classic.render(view).text.replace("\u2060", "")  # word joiners break path links
    assert f"<code>{CID}</code>" in shown and "runatlantis/atlantis#2607" in shown
    assert "44 candidates · created 1" in shown and "stale_issue ×14 · too_big:m ×7 · labels ×3" in shown
    assert "language ×1" not in shown  # the top 3 only
    assert view.rows == [] and view.kind == "fyi"


def test_cycle_fyi_without_new_items(ctx: AppContext) -> None:
    view = discovery_fyi.cycle_view(report(ctx, scope="repos", drops={}), [])
    goldens("discovery_cycle_empty", view)
    shown = classic.render(view).text
    assert "No new items this cycle" in shown and "(ossctl discover)" in shown and "Top drops" not in shown


def test_a_cid_without_a_gatekeeper_row_is_not_shown(ctx: AppContext) -> None:
    assert discovery_fyi._rows(ctx, ["c_0000000000"]) == []


def test_untrusted_text_is_redacted_and_escaped(ctx: AppContext) -> None:
    rows = created_row(ctx, upstream="acme/<b>bold</b>", number=1)
    view = discovery_fyi.cycle_view(report(ctx, created=[CID], created_total=1, drops={GHP_LOWER: 2}), rows)
    shown = classic.render(view).text
    assert GHP_LOWER not in shown and "[REDACTED]" in shown
    assert "<b>bold" not in shown and "&lt;b&gt;bold" in shown
    dumped = json.dumps(blocks.dump(rich.render(replace(view, rich=True))))
    assert GHP_LOWER not in dumped


async def test_the_fyi_is_a_silent_p2_held_in_quiet_hours(ctx: AppContext) -> None:
    notifier = Notifier(ctx)
    ctx.outbox.hold_policy = notifier.decide
    notifier.prefs.set("quiet_until", to_iso(ctx.clock.now() + timedelta(hours=1)))
    row_id = discovery_fyi.record(ctx, report(ctx), WORKER)
    row = db.one(ctx.conn, "SELECT priority, status, thread_key FROM tg_outbox WHERE id = ?", (row_id,))
    assert row is not None and tuple(row) == (2, "held", "feed")
    assert ctx.outbox.due() == []


def test_a_cut_is_audited_without_a_message(ctx: AppContext) -> None:
    assert discovery_fyi.record(ctx, report(ctx, outcome="cut", cut_reason="core_limit"), WORKER) is None
    assert ctx.conn.execute("SELECT COUNT(*) FROM tg_outbox").fetchone()[0] == 0
    assert ctx.audit.count_since("discovery_cycle", "2000-01-01") == 1


async def test_delivered_through_the_rig(rig: TgRig) -> None:
    ctx = rig.ctx
    insert_contribution(ctx, State.DISCOVERED, upstream="runatlantis/atlantis", issue_number=2607, id=CID)
    discovery_fyi.record(ctx, report(ctx, created=[CID], created_total=1), WORKER)
    await rig.flush()  # the copyable-id lint (idcheck) runs on every payload here
    mid = sent_with(rig, "Discovery")
    assert f"<code>{CID}</code>" in text(rig, mid) and labels(rig, mid) == []
    row = ctx.conn.execute("SELECT payload_json FROM tg_outbox WHERE dedupe_key LIKE 'discovery_cycle:%'").fetchone()
    assert json.loads(row[0]).get("disable_notification") is True


# ------------------------------------------------------------------ the stall warning
def test_stalled_classic_and_rich() -> None:
    last = datetime(2026, 10, 22, 1, 30, tzinfo=UTC)
    goldens("discovery_stalled", discovery_fyi.stalled_view(last, Counter({"search_limit": 71, "core_limit": 14})))
    goldens("discovery_stalled_never", discovery_fyi.stalled_view(None, Counter()))
    view = discovery_fyi.stalled_view(None, Counter())
    assert view.rows == [] and "none since the gatekeeper started" in classic.render(view).text


def stalled(ctx: AppContext) -> list[Any]:
    return list(ctx.conn.execute("SELECT priority, thread_key, dedupe_key FROM tg_outbox"
                                 " WHERE dedupe_key LIKE 'discovery_stalled:%' ORDER BY id"))  # fmt: skip


async def test_the_stall_warning_fires_once_per_episode(ctx: AppContext, clock: FakeClock) -> None:
    assert await discovery_fyi.watch(ctx) is None  # the gatekeeper's start is the baseline
    clock.advance(timedelta(hours=11))
    assert await discovery_fyi.watch(ctx) is None  # not before 12 h
    clock.advance(timedelta(hours=1, minutes=1))
    assert await discovery_fyi.watch(ctx) is not None
    clock.advance(timedelta(minutes=6))
    assert await discovery_fyi.watch(ctx) is None  # the same episode
    ((priority, thread, key),) = stalled(ctx)
    assert (priority, thread) == (1, "feed") and key.startswith("discovery_stalled:start:")
    discovery_fyi.record(ctx, report(ctx), WORKER)  # a finished full cycle ends the episode
    clock.advance(timedelta(hours=11))
    assert await discovery_fyi.watch(ctx) is None
    discovery_fyi.record(ctx, report(ctx, outcome="cut", cut_reason="search_limit"), WORKER)
    discovery_fyi.record(ctx, report(ctx, scope="repos"), WORKER)  # ossctl discover does not count
    clock.advance(timedelta(hours=1, minutes=6))
    assert await discovery_fyi.watch(ctx) is not None
    assert len(stalled(ctx)) == 2
    row = ctx.conn.execute("SELECT payload_json FROM tg_outbox WHERE dedupe_key = ?", (stalled(ctx)[-1][2],)).fetchone()
    assert "search limit ×1" in row[0]


@pytest.mark.parametrize("stop", ["paused", "killed", "draining"])
async def test_no_stall_warning_while_stopped(ctx: AppContext, clock: FakeClock, stop: str) -> None:
    assert await discovery_fyi.watch(ctx) is None
    if stop == "paused":
        ctx.control.pause("test", "operator")
    elif stop == "killed":
        ctx.control.kill("test", "operator")
    else:
        ctx.control.drain("operator", "test", 60)
    clock.advance(timedelta(hours=13))
    assert await discovery_fyi.watch(ctx) is None
    assert stalled(ctx) == []
