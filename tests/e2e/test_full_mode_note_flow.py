"""ADR-0025 volume note end to end (build-env design §2.3, §5.1, §5.2): in egress mode ``sandbox_full`` a web
validate job that uploads 80 MB (over ``max_upload_mb``) is not stopped and its contribution is not held; sandboxd
posts an egress note, the gatekeeper stores it (no transition, no card), the stage succeeds, and the daily digest
shows the "🌐 Egress notes" block. The same upload in ``sandbox_open`` still holds (the ADR-0023 guard)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.egress_notes import EgressNotes
from ossbot.gatekeeper.telegram import digest
from ossbot.gatekeeper.telegram.ui import classic
from tests.e2e.conftest import build_e2e
from tests.e2e.test_floor_hold_flow import Rig, floor_meter, stage_runs, validated
from tests.githarness import GitWorld
from tests.telegram.tgharness import make_rig

pytestmark = pytest.mark.timeout(300)
MIB = 1024 * 1024
UPLOAD = f"TS CLIENT 3128 TCP_TUNNEL/200 CONNECT paste.example:443 1.2.3.4 {80 * MIB} 4000 900 -\n"


@pytest.mark.parametrize("mode", ["sandbox_full", "sandbox_open"])
async def test_a_volume_breach_is_a_note_only_in_sandbox_full(gctx: AppContext, world: GitWorld, tmp_path: Path,
                                                              mode: str) -> None:  # fmt: skip
    with build_e2e(gctx, world, tmp_path) as e2e:
        async for tg in make_rig(gctx):
            rig = Rig(e2e, tg)
            default = gctx.config.egress.mode_default
            gctx.control.set_egress_mode(mode, "uid:1000/operator", default, egress=gctx.config.egress)  # type: ignore[arg-type]
            e2e.jobs.egress_mode = e2e.jobs.exec_egress_mode  # the summary label: the same live flag
            cid = e2e.discover_one()
            meter = floor_meter(rig, tmp_path, cid, UPLOAD)
            meter.current_mode = e2e.jobs.exec_egress_mode  # sandboxd's own read of the live flag
            e2e.start_work(cid)
            scheduler = e2e.ready_scheduler()
            for _ in range(40):
                if validated(gctx, cid) or gctx.holds.is_held(cid):
                    break
                scheduler.tick()
                gctx.clock.advance(seconds=20)  # type: ignore[attr-defined]
            notes = EgressNotes(gctx.conn, gctx.clock, gctx.audit).since(None, cid)
            if mode == "sandbox_open":
                assert gctx.holds.is_held(cid) and notes == [], e2e.why(cid)
                return
            assert validated(gctx, cid), e2e.why(cid)
            assert not gctx.holds.is_held(cid)
            assert [r for r in stage_runs(gctx, cid) if r[1] == "failed"] == []
            assert len(notes) == 1 and notes[0].kind == "volume" and notes[0].summary.breached == ["upload"]
            assert notes[0].summary.egress_mode == "sandbox_full" and notes[0].stage == "validate"
            assert (
                db.one(gctx.conn, "SELECT COUNT(*) FROM tg_outbox WHERE payload_json LIKE '%Egress anomaly%'")[0] == 0
            )
            gctx.clock.advance(timedelta(minutes=1))  # type: ignore[attr-defined]
            period = digest.now_period(gctx, "daily")
            stats = digest.collect(gctx, period, {})
            block = digest.egress_notes_block(stats["egress_notes"])
            assert block is not None and stats["egress_notes"]["count"] == 1
            shown = classic.render(digest.view(gctx, period, stats, {})).text
            assert "Egress notes" in shown and "↑ 80 MB (&gt; " in shown
            assert e2e.writes() == []
