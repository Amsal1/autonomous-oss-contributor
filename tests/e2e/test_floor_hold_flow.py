"""ADR-0025 floor hold end to end (build-env design §4.5, §5.1, §5.2): sandboxd's meter sees a validate job's
squid line marked ``dst`` (an allowed name that resolved into a private range), stops the job and asks for an
``egress_floor`` hold; the gatekeeper holds only that contribution with the P1 floor card, the runner treats the
stop as ``ContributionHeld`` (cancelled, never failed), and the owner's two-tap Resume lets the stage run again.
Every component is real (scheduler, stages, gatekeeper, sandboxd on the fake runtime, the Telegram handlers over
the fake Bot API); GitHub is ``tests/fakes/fake_github.py`` and records no write; dry-run is never touched."""

from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.sandboxd.egress_meter import EgressMeter
from tests.e2e.conftest import E2E, build_e2e
from tests.githarness import GitWorld
from tests.telegram.reviewkit import sent_with, token
from tests.telegram.tgharness import TgRig, make_rig

pytestmark = pytest.mark.timeout(300)


class Rig:
    def __init__(self, e2e: E2E, tg: TgRig) -> None:
        self.e2e, self.tg = e2e, tg

    @property
    def ctx(self) -> AppContext:
        return self.e2e.ctx

    async def tap(self, mid: int, label: str) -> None:
        await self.tg.tap(token(self.tg, mid, label), message=mid)


@pytest.fixture
async def rig(gctx: AppContext, world: GitWorld, tmp_path: Path) -> AsyncIterator[Rig]:
    with build_e2e(gctx, world, tmp_path) as e2e:
        async for tg in make_rig(gctx):
            assert db.current_dry_run(gctx.conn) is True
            yield Rig(e2e, tg)


def floor_meter(r: Rig, tmp_path: Path, cid: str, line: str) -> EgressMeter:
    """A meter on a temp squid log; the first validate job of ``cid`` writes ``line`` (CLIENT = its address)."""
    log = tmp_path / "proxy-access.log"
    log.write_text("", encoding="utf-8")
    jobs = r.e2e.jobs
    meter = EgressMeter(log, tmp_path / "egress-runs", lambda: r.ctx.config.egress.anomaly, sleep=lambda s: None,
                        on_breach=jobs.flag_anomaly, on_floor=jobs.flag_floor, on_note=jobs.post_note)  # fmt: skip
    meter.prime()
    register = meter.register
    done: list[str] = []

    def with_traffic(run_id: str, job_cid: str, stage: str, subnet: str, start_ts: float, egress: str,
                     mode: str) -> None:  # fmt: skip
        register(run_id, job_cid, stage, subnet, start_ts, egress, mode)
        if job_cid == cid and stage == "validate" and not done:
            done.append(run_id)
            client = str(next(ipaddress.IPv4Network(subnet).hosts()))
            with log.open("a", encoding="utf-8") as fh:
                fh.write(line.replace("CLIENT", client).replace("TS", f"{start_ts + 1:.3f}"))

    meter.register = with_traffic  # type: ignore[method-assign]
    jobs.meter = meter
    return meter


def stage_runs(ctx: AppContext, cid: str) -> list[tuple[str, str, str | None]]:
    sql = "SELECT stage, status, error_class FROM stage_runs WHERE cid = ? ORDER BY started_at, rowid"
    return [(r[0], r[1], r[2]) for r in ctx.conn.execute(sql, (cid,))]


def validated(ctx: AppContext, cid: str) -> bool:
    return ("validate", "succeeded", None) in stage_runs(ctx, cid)


async def test_a_floor_attempt_holds_the_contribution(rig: Rig, tmp_path: Path) -> None:
    cid = rig.e2e.discover_one()
    floor_meter(rig, tmp_path, cid,
                "TS CLIENT 3128 TCP_DENIED/403 CONNECT repo.maven.apache.org:443 - 0 0 1 dst\n")  # fmt: skip
    rig.e2e.start_work(cid)
    scheduler = rig.e2e.ready_scheduler()
    for _ in range(40):
        if rig.ctx.holds.is_held(cid):
            break
        scheduler.tick()
        rig.ctx.clock.advance(seconds=20)  # type: ignore[attr-defined]
    hold = rig.ctx.holds.get_open(cid)
    assert hold is not None, rig.e2e.why(cid)
    assert hold.reason == "egress_floor" and hold.detail.breached == []
    assert (hold.detail.floor.cls, hold.detail.floor.target, hold.detail.stage) == (
        "dst", "repo.maven.apache.org", "validate")  # fmt: skip
    assert ("validate", "cancelled", "contribution_held") in stage_runs(rig.ctx, cid)
    assert [r for r in stage_runs(rig.ctx, cid) if r[1] == "failed"] == []
    assert rig.e2e.state(cid) == "VALIDATING" and rig.e2e.jobs.pending_holds() == []
    card = db.one(rig.ctx.conn, "SELECT priority FROM tg_outbox WHERE payload_json LIKE '%Floor reach attempt%'")
    assert card is not None and card["priority"] == 1
    await rig.tg.flush()
    mid = sent_with(rig.tg, "Floor reach attempt")
    await rig.tap(mid, "▶️ Resume…")
    assert rig.ctx.holds.is_held(cid)  # one tap is not a decision
    await rig.tap(mid, "▶️ Resume")
    assert not rig.ctx.holds.is_held(cid)
    for _ in range(10):
        if validated(rig.ctx, cid):
            break
        scheduler.tick()
        rig.ctx.clock.advance(seconds=20)  # type: ignore[attr-defined]
    assert validated(rig.ctx, cid), rig.e2e.why(cid)
    assert rig.e2e.writes() == [] and db.current_dry_run(rig.ctx.conn) is True
