"""The README demo capture (``scripts/dev/make-demo.ps1``): the hermetic e2e dry-run flow on the fictional
upstream ``acme/widgets`` (fake GitHub, local upstream, test profile) driven through the real Telegram
handlers over a recording fake Bot API, plus the fake Hermes stream.

The transcript holds every Bot API call the gatekeeper made, so the demo media show only real renderer
output. ``OSSBOT_DEMO_OUT`` set: the transcript is written there; either way it is replayed and every
scene page is rendered and checked against the captured payloads (no browser needed).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ossbot.common import ids
from ossbot.common.paths import Paths
from ossbot.gatekeeper import db
from ossbot.gatekeeper.chat import drafts, stream, turns
from ossbot.gatekeeper.chat.drafts import DraftLane
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import status_card
from ossbot.gatekeeper.telegram.runtime import ui_of
from ossbot.runner.backends import base as runner_base
from tests import githarness
from tests.conftest import make_repo
from tests.e2e.conftest import E2E, build_e2e
from tests.e2e.demo_capture import (
    CaptureBotRequest,
    DemoReader,
    FakeMono,
    FakeSecrets,
    FakeTime,
    PacedBackend,
    Recorder,
)
from tests.fakes import fake_telegram
from tests.githarness import GitWorld, LocalRemote
from tests.telegram import proposalkit, reviewkit
from tests.telegram.chatkit import deltas, wire
from tests.telegram.tgharness import TgRig, make_rig
from tests.unit.gatekeeper.test_submit import submitter

pytestmark = pytest.mark.timeout(300)

DEMO_REPO = "acme/widgets"  # fictional: the demo never shows a made-up contribution to a real project
DEMO_DATE = "2026-10-17T12:00:00Z"
DEMO_PROJECT_URL = "https://example.org/autonomous-oss-contributor"
DEMO_ROOT = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "demo"
QUESTION = "How do the two approval gates work?"
ANSWER = (
    "ossbot asks you twice. **Gate 1** is the proposal card: the plan and a size estimate, before any "
    "implementation compute is spent.",
    "\n\n**Gate 2** is the PR preview, built by the gatekeeper from the exact commit it would push. "
    "Both gates take two taps, and the confirm button names the mode.",
    "\n\nDry-run is on by default, so a confirm publishes nothing until you turn it off.",
)
TARGET = (
    '  - id: demo\n    priority: 1\n    why: "Fictional demo upstream"\n    repos:\n      - { repo: acme/widgets }\n'
)


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    repo = make_repo(tmp_path)
    targets = repo / "config" / "targets.yaml"
    text = targets.read_text(encoding="utf-8")
    assert "\ndenylist:" in text
    targets.write_text(text.replace("\ndenylist:", "\n" + TARGET + "denylist:", 1), encoding="utf-8")
    # the PR footer links a fictional URL: the media carry no owner value, not even the project URL's login
    policy = repo / "config" / "policy.yaml"
    ptext = policy.read_text(encoding="utf-8")
    assert "\ntext:\n" in ptext
    policy.write_text(ptext.replace("\ntext:\n", f"\ntext:\n  project_url: {DEMO_PROJECT_URL}\n", 1), encoding="utf-8")
    p = Paths.under(tmp_path, repo=repo)
    for d in (p.control_dir, p.logs_dir, p.snapshot_root, p.gatekeeper_dir, p.run):
        d.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def dated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """The harness's own commits (the upstream) get a fixed date, so their SHAs repeat."""
    real_env = githarness.harness_env

    def dated_env(home: Path) -> dict[str, str]:
        return {**real_env(home), "GIT_AUTHOR_DATE": DEMO_DATE, "GIT_COMMITTER_DATE": DEMO_DATE}

    monkeypatch.setattr(githarness, "harness_env", dated_env)


@pytest.fixture
def world(tmp_path: Path, remote: LocalRemote, dated_git: None) -> GitWorld:
    return GitWorld(tmp_path / "world", remote, upstream=DEMO_REPO)


@pytest.fixture
def fixed(gctx: AppContext, world: GitWorld, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fixed clocks and ids for this test only (so two captures are byte-identical)."""
    secrets = FakeSecrets()
    monkeypatch.setattr(ids, "time", FakeTime(gctx.clock))  # type: ignore[arg-type]
    monkeypatch.setattr(ids, "_last", (0, 0))
    for module in (ids, turns, runner_base):
        monkeypatch.setattr(module, "secrets", secrets)
    monkeypatch.setattr(fake_telegram, "_update_ids", iter(range(1, 10**6)))
    defaults = DraftLane.__init__.__kwdefaults__
    assert defaults is not None
    monkeypatch.setitem(defaults, "monotonic", FakeMono(step=2.0))
    monkeypatch.setattr(drafts, "TICK_S", 0.0)
    monkeypatch.setattr(stream.StreamState, "elapsed_s", property(lambda self: 6))
    DraftLane._last_by_chat.clear()
    yield
    DraftLane._last_by_chat.clear()


# ------------------------------------------------------------------ driving helpers
def topics(ctx: AppContext) -> dict[str, int]:
    return {str(r["name"]): int(r["thread_id"]) for r in ctx.conn.execute("SELECT * FROM tg_topics")}


def proposal_message(ctx: AppContext, cid: str) -> int:
    row = db.one(ctx.conn, "SELECT id FROM proposal_approvals WHERE cid = ? AND status = 'pending'", (cid,))
    assert row is not None, cid
    return proposalkit.card_of(ctx, str(row[0])).message_id


async def tap(rig: TgRig, message_id: int, label: str) -> None:
    await rig.tap(reviewkit.token(rig, message_id, label), message=message_id)


def bot_sends(rig: TgRig, after: int) -> list[dict[str, Any]]:
    req = rig.req
    assert isinstance(req, CaptureBotRequest)
    return [e for e in req.events[after:] if e["type"] == "bot" and e["method"] in {"sendMessage", "sendRichMessage"}]


async def to_proposal(h: E2E, rig: TgRig) -> tuple[str, int]:
    h.ready_scheduler().refused.clear()
    created = h.ready_scheduler().discover([DEMO_REPO])
    assert len(created) == 1, created
    cid = created[0]
    assert h.run_until(cid, "PROPOSED") == "PROPOSED", h.why(cid)
    await rig.flush()
    return cid, proposal_message(h.ctx, cid)


async def start_and_preview(h: E2E, rig: TgRig, cid: str, mid: int) -> reviewkit.Card:
    await tap(rig, mid, "✅ Start work")
    await tap(rig, mid, "🛠 Confirm")
    assert h.run_until(cid, "AWAITING_APPROVAL") == "AWAITING_APPROVAL", h.why(cid)
    await rig.flush()
    return reviewkit.card_of(h.ctx, h.pending_approval(cid))


async def drive(h: E2E, rig: TgRig, rec: Recorder) -> None:
    ctx = h.ctx
    req = rig.req
    assert isinstance(req, CaptureBotRequest)
    # 1. proposal card (gate 1)
    cid, pmid = await to_proposal(h, rig)
    rec.mark("01-proposal", [pmid])
    rec.mark("01-proposal-press", [pmid], pressed="✅ Start work")
    # 2. Start work -> Confirm
    await tap(rig, pmid, "✅ Start work")
    rec.mark("02-confirm-panel", [pmid], pressed="🛠 Confirm")
    await tap(rig, pmid, "🛠 Confirm")
    rec.mark("02-started", [pmid])
    # 3. PR preview card
    assert h.run_until(cid, "AWAITING_APPROVAL") == "AWAITING_APPROVAL", h.why(cid)
    await rig.flush()
    card = reviewkit.card_of(ctx, h.pending_approval(cid))
    rec.mark("03-preview", [card.message_id])
    # 4. diff explorer
    await tap(rig, card.message_id, "🧭 Explore")
    explorer = reviewkit.view_message(ctx, "explorer")
    rec.mark("04-explorer", [card.message_id, explorer])
    await tap(rig, explorer, "1/9 ▾")
    await tap(rig, explorer, "6")
    rec.mark("04-diff", [card.message_id, explorer])
    # 5. Approve -> Confirm (DRY RUN)
    rec.mark("05-press-approve", [card.message_id], pressed="✅ Approve")
    await tap(rig, card.message_id, "✅ Approve")
    rec.mark("05-dry-run-panel", [card.message_id], pressed="🧪 Confirm dry run")
    await tap(rig, card.message_id, "🧪 Confirm dry run")
    rec.mark("05-dry-run-done", [card.message_id])
    assert h.state(cid) == "DRY_RUN_DONE" and h.writes() == []
    # 6. LIVE against the fake GitHub (this test's own database only), then the live status card
    ctx.control.set_dry_run(False, "uid:1000/operator")
    await rig.flush()
    cid_b, pmid_b = await to_proposal(h, rig)
    card_b = await start_and_preview(h, rig, cid_b, pmid_b)
    await tap(rig, card_b.message_id, "✅ Approve")
    rec.mark("06-live-panel", [card_b.message_id], pressed="🚀 Confirm LIVE")
    await tap(rig, card_b.message_id, "🚀 Confirm LIVE")
    assert h.state(cid_b) != "AWAITING_APPROVAL", h.why(cid_b)
    _, head, branch = h.pusher.pushes[-1]
    h.world.push_fork(ctx.paths.artifacts_dir / cid_b / "1" / "change.bundle", head, branch)
    assert h.run_until(cid_b, "WATCHING") == "WATCHING", h.why(cid_b)
    assert await status_card.refresh(ctx, DemoReader(h.gh), cid_b)
    await rig.flush()
    status = reviewkit.view_message(ctx, "status")
    rec.mark("06-submitted", [card_b.message_id, status])
    ctx.control.set_dry_run(True, "uid:1000/operator")  # back to the default for the rest of the tour
    await rig.flush()
    # 7. pinned dashboard and /status (in Control); the pinned one as housekeeping keeps it
    control = topics(ctx)["control"]
    ui_of(ctx).dashboard.tick(force=True)
    await rig.flush()
    mark = len(req.events)
    uid, update = rec.user("/status", thread_id=control)
    await rig.send(update)
    reply = bot_sends(rig, mark)
    assert reply, "no /status reply"
    rec.mark("07-status", [uid, int(reply[-1]["result"]["message_id"])])
    # 8. the assistant: a streamed answer (fake Hermes stream)
    wire(ctx, PacedBackend(deltas(*ANSWER), req.drafts, rig.frontend.worker.run_once))
    assistant = topics(ctx)["assistant"]
    mark = len(req.events)
    uid, update = rec.user(QUESTION, thread_id=assistant)
    await rig.send(update)
    draft_events = [e for e in req.events[mark:] if e["type"] == "bot" and e["method"].endswith("Draft")]
    assert len(draft_events) >= len(ANSWER) + 1, [e.get("method") for e in req.events[mark:]]
    final = bot_sends(rig, mark)
    assert final, "no final answer"
    for i, event in enumerate(draft_events):
        rec.mark(f"08-draft-{i}", [uid], upto=int(event["seq"]), draft=int(event["params"]["draft_id"]))
    rec.mark("08-answer", [uid, int(final[-1]["result"]["message_id"])])
    # 9. /kill
    mark = len(req.events)
    uid, update = rec.user("/kill", thread_id=control)
    await rig.send(update)
    reply = bot_sends(rig, mark)
    assert reply and ctx.control.view().state == "killed"
    ui_of(ctx).dashboard.tick(force=True)
    await rig.flush()
    rec.mark("09-kill", [uid, *(int(r["result"]["message_id"]) for r in reply)])


async def test_demo_capture(gctx: AppContext, world: GitWorld, tmp_path: Path, fixed: None) -> None:
    out_dir = Path(os.environ["OSSBOT_DEMO_OUT"]) if os.environ.get("OSSBOT_DEMO_OUT") else tmp_path / "demo"
    req = CaptureBotRequest(gctx.clock, topics=True)
    rec = Recorder(req)
    with build_e2e(gctx, world, tmp_path, repo=DEMO_REPO) as h:
        sub = submitter(gctx, h.gh, h.pusher)
        async for rig in make_rig(gctx, topics=True, req=req, submit=sub.submit):
            await drive(h, rig, rec)
        live = [w for w in h.writes() if "POST" in str(w)]
        assert live, "the LIVE run made no write on the fake GitHub"
    transcript = out_dir / "transcript.json"
    rec.write(transcript)
    doc = json.loads(transcript.read_text(encoding="utf-8"))
    marks = [e["name"] for e in doc["events"] if e["type"] == "mark"]
    assert [m[:2] for m in marks] == sorted(m[:2] for m in marks)
    assert {m[:2] for m in marks} == {f"{i:02d}" for i in range(1, 10)}
    text = json.dumps(doc, ensure_ascii=False)
    for needle in ("/tmp/", "pytest-of-", "/home/", "/mnt/"):
        assert needle not in text, needle
    # replay it through the demo renderer: every scene page, both themes, checked against the payloads
    if str(DEMO_ROOT) not in sys.path:
        sys.path.insert(0, str(DEMO_ROOT))
    from demokit import pages
    from demokit import transcript as tr

    loaded = tr.load(transcript)
    built = pages.build(loaded)
    assert len({p.entry["scene"] for p in built}) == 9
    by_mark = {p.entry["mark"]: p for p in built}
    assert "🧪 DRY RUN" in by_mark["05-dry-run-panel"].html and "Killed" in by_mark["09-kill"].html
    assert "Submitted · PR #" in by_mark["06-submitted"].html
