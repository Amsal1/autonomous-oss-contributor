"""Runner scheduler (DESIGN §9.1-§9.3, §9.14, §10.9 M11, §13.5 CR-001, §16.3).

Each tick: nothing while killed, paused, draining or down (the gate); first start runs the M11
order (catalog -> safety-probe status -> first stage); a claimed ``ossctl models refresh`` or a stale
catalog runs the catalog job; discovery every ``discovery.interval_hours``; then every contribution
whose state has a stage, within ``limits.max_concurrent_pipelines`` and
``proposal.max_concurrent_scouts`` (one stage per contribution at a time). Stage failures move the
item to FAILED (the gatekeeper redirects to WATCHING while a PR is open). On start, stage runs left
``running`` by a crashed runner become ``interrupted`` and their stages are re-queued (max 3 by the
gatekeeper); ``resumed`` counts them.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

from ossbot.common.clock import from_iso
from ossbot.runner import stages
from ossbot.runner.gk_client import GkError
from ossbot.runner.llm import StageFailed
from ossbot.runner.pipeline import Deferred, Interrupted, StageContext
from ossbot.runner.sbx_client import SbxError
from ossbot.runner.stages import catalog, discover, scout

log = logging.getLogger(__name__)
TERMINAL = frozenset({"SKIPPED", "ABANDONED", "REJECTED", "EXPIRED", "FAILED", "DRY_RUN_DONE", "MERGED", "CLOSED",
                      "PROPOSAL_REJECTED"})  # fmt: skip
NO_FAIL = frozenset({"PROPOSED", "RESCOPE_REQUIRED", "AWAITING_APPROVAL", "APPROVED", "SUBMITTING", "SUBMITTED",
                     "SUBMIT_FAILED"})  # fmt: skip  # G/H-owned: a stage error never fails them from the runner


@dataclasses.dataclass
class Tick:
    started: list[str] = dataclasses.field(default_factory=list)
    skipped: str | None = None


class Scheduler:
    def __init__(
        self, sc: StageContext, *, now: Callable[[], datetime] = lambda: datetime.now(UTC), workers: int = 4,
        synchronous: bool = False,
    ) -> None:  # fmt: skip
        self.sc = sc
        self.now = now
        self.synchronous = synchronous  # tests: run stages inline, in order
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="stage")
        self.running: dict[str, tuple[str, Future[None] | None]] = {}  # cid -> (stage name, future)
        self.lock = threading.Lock()
        self.first_start_done = False
        self.last_discovery: datetime | None = None
        self.last_catalog: datetime | None = None
        self.refused: set[str] = set()
        self.resumed = 0

    # ------------------------------------------------------------------ start
    def recover_interrupted(self) -> int:
        """Stage runs still ``running`` at start belong to a dead runner: interrupted, re-queued."""
        count = 0
        for run in self.sc.gk.stage_runs(status="running"):
            self.sc.gk.stage_update(str(run["id"]), status="interrupted", error_class="interrupted",
                                    error_message="runner restarted")  # fmt: skip
            count += 1
        self.resumed = count
        return count

    def first_start(self) -> None:
        """M11: catalog (when empty or stale) -> probe status -> stages."""
        rows = self.sc.gk.catalog()
        if not [r for r in rows if not r.get("forced")]:
            self._catalog()
        try:
            status = self.sc.sbx.probes_status()
            if not status.get("all_passed"):
                log.warning("safety probes not passed for the current tuple; Kiro jobs wait for them",
                            extra={"event": "probes_pending"})  # fmt: skip
        except SbxError as exc:
            log.warning("probe status unavailable: %s", exc.code, extra={"event": "probes_unavailable"})
        self.first_start_done = True

    def _catalog(self) -> None:
        try:
            catalog.run(self.sc)
        except (StageFailed, SbxError, GkError, Interrupted) as exc:
            log.warning("catalog job failed: %s", exc, extra={"event": "catalog_failed"})
        self.last_catalog = self.now()

    # ------------------------------------------------------------------- tick
    def tick(self) -> Tick:
        out = Tick()
        if not self.sc.gate.open("discover"):
            out.skipped = "stopped"  # killed, paused, draining or down: nothing new starts
            return out
        bundle = self.sc.config()
        if not self.first_start_done:
            self.first_start()
        refresh_h = self.sc.models.get().catalog.refresh_hours
        stale = self.last_catalog is None or self.now() - self.last_catalog > timedelta(hours=refresh_h)
        try:
            claimed = self.sc.gk.claim_refresh()
        except GkError:
            claimed = False
        if claimed or stale:
            self._catalog()
        interval = timedelta(hours=bundle.policy.discovery.interval_hours)
        if self.last_discovery is None or self.now() - self.last_discovery >= interval:
            self.discover()
        self._dispatch(out)
        return out

    def discover(self, repos: list[str] | None = None) -> list[str]:
        if repos is None:
            self.last_discovery = self.now()
            self.refused.clear()  # refusals are cached for one discovery cycle
        try:
            return discover.run(self.sc, repos=repos, refused=self.refused)
        except (Interrupted, GkError) as exc:
            log.warning("discovery skipped: %s", exc, extra={"event": "discover_skipped"})
            return []

    def _eligible(self, c: dict[str, Any]) -> bool:
        state = str(c.get("state"))
        if state == "PROPOSAL_SNOOZED":
            until = c.get("snooze_until")
            return until is None or from_iso(str(until)) <= self.now()
        if state == "PROPOSAL_EXPIRED":  # re-scouted after the 7-day rule if still relevant
            days = self.sc.config().policy.limits.rediscover_cooldown_days
            return self.now() - from_iso(str(c["updated_at"])) >= timedelta(days=days)
        if state == "WATCHING":
            from ossbot.runner.stages import watch  # noqa: PLC0415

            return watch.due(self.sc, c)
        return True

    def _dispatch(self, out: Tick) -> None:
        bundle = self.sc.config()
        max_pipelines = bundle.policy.limits.max_concurrent_pipelines
        max_scouts = bundle.policy.proposal.max_concurrent_scouts
        with self.lock:
            self.running = {cid: v for cid, v in self.running.items() if v[1] is None or not v[1].done()}
        for c in self.sc.gk.contributions(limit=200):
            cid, state = str(c["id"]), str(c.get("state"))
            if state in TERMINAL or cid in self.running:
                continue
            stage = stages.stage_for(c)
            if stage is None or not self._eligible(c):
                continue
            name = "scout" if stage is scout.run else "pipeline"
            busy = sum(1 for v in self.running.values() if v[0] == name)
            if (name == "scout" and busy >= max_scouts) or (name == "pipeline" and busy >= max_pipelines):
                continue
            out.started.append(cid)
            if self.synchronous:
                self.running[cid] = (name, None)
                self.run_one(cid)
                self.running.pop(cid, None)
            else:
                self.running[cid] = (name, self.pool.submit(self.run_one, cid))

    # --------------------------------------------------------------- one stage
    def run_one(self, cid: str) -> None:
        sc = dataclasses.replace(self.sc, run=None)  # per-thread stage context
        try:
            c = sc.gk.contribution(cid)
            stage = stages.stage_for(c)
            if stage is None:
                return
            stage(sc, c)
        except (Interrupted, Deferred) as exc:
            log.info("stage stopped for %s: %s", cid, exc, extra={"event": "stage_stopped", "cid": cid})
        except StageFailed as exc:
            self.fail(sc, cid, exc.error_class)
        except GkError as exc:
            if exc.status == 409 and exc.code in {"stale_from", "stage_running"}:
                return  # someone else moved it; the next tick re-reads
            if exc.status == 423:
                return  # stopped meanwhile
            log.warning("gatekeeper refused %s: %s", cid, exc.code, extra={"event": "stage_refused", "cid": cid})
            self.fail(sc, cid, f"gk_{exc.code}")
        except SbxError as exc:
            if exc.status == 423:
                return  # paused/killed/draining, or safety probes missing: retried later
            self.fail(sc, cid, f"sbx_{exc.code}")
        except Exception as exc:  # pragma: no cover - logged, the item fails rather than looping
            log.exception("stage crashed for %s", cid, extra={"event": "stage_crashed", "cid": cid})
            self.fail(sc, cid, f"internal:{type(exc).__name__}")

    def fail(self, sc: StageContext, cid: str, error_class: str) -> None:
        try:
            state = str(sc.gk.contribution(cid)["state"])
            if state in TERMINAL or state in NO_FAIL:
                return
            sc.gk.transition(cid, state, "FAILED", {"error_class": error_class[:64]})
            if error_class == "model_unavailable":
                sc.say(f"⚠️ {cid}: no model of its role is in the Kiro catalog (model_unavailable).")
        except GkError as exc:
            log.warning("could not fail %s: %s", cid, exc.code, extra={"event": "fail_refused", "cid": cid})

    def shutdown(self, wait: bool = True) -> None:
        self.pool.shutdown(wait=wait, cancel_futures=True)
