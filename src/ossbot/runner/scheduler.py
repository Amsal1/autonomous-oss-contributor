"""Runner scheduler (DESIGN §9.1-§9.3, §9.14, §10.9 M11, §13.5 CR-001, §16.3).

Each tick: nothing while killed, paused, draining or down (the gate); then the M11 order, at
first start and whenever it is not complete: catalog (``list_models`` -> upsert) -> safety-probe
status -> ``POST /v1/probes/run`` with the resolved planner and coder models when the tuple has no
full pass -> stages. Until both hold, no discovery and no stage runs (no transition, no stage
run). A failed or empty catalog alerts ``catalog_empty`` once and is retried hourly; probes that
did not pass are retried after ``PROBE_RETRY``; a Kiro job refused by sandboxd's probe gate (423)
re-opens the probe step instead of failing or re-running the item. A claimed ``ossctl models
refresh`` or a stale catalog runs the catalog job (the last good catalog stays on failure).
Discovery every ``discovery.interval_hours``; then every contribution whose state has a stage,
within ``limits.max_concurrent_pipelines`` and ``proposal.max_concurrent_scouts`` (one stage per
contribution at a time); autonomous first scouts wait while ``proposal.max_per_day`` is used up.
Stage failures move the item to FAILED (the gatekeeper redirects to WATCHING while a PR is open);
interruptions (control state, a sandbox job cut by a drain or crash, sandboxd unreachable) only
end the stage run as ``interrupted`` and the next tick re-queues it (max 3 by the gatekeeper). On
start, stage runs left ``running`` by a crashed runner become ``interrupted``; ``resumed`` counts
them (``ossctl up`` reports it).
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
from ossbot.common.models import Catalog, ModelUnavailable
from ossbot.runner import stages
from ossbot.runner.errors import CapReached, Deferred, Interrupted, ProbesNotReady, StageFailed
from ossbot.runner.gk_client import GkError
from ossbot.runner.pipeline import StageContext
from ossbot.runner.sbx_client import SbxError
from ossbot.runner.stages import catalog, discover, scout

log = logging.getLogger(__name__)
TERMINAL = frozenset({"SKIPPED", "ABANDONED", "REJECTED", "EXPIRED", "FAILED", "DRY_RUN_DONE", "MERGED", "CLOSED",
                      "PROPOSAL_REJECTED"})  # fmt: skip
NO_FAIL = frozenset({"PROPOSED", "RESCOPE_REQUIRED", "AWAITING_APPROVAL", "APPROVED", "SUBMITTING", "SUBMITTED",
                     "SUBMIT_FAILED"})  # fmt: skip  # G/H-owned: a stage error never fails them from the runner
CATALOG_RETRY = timedelta(hours=1)  # §20: catalog_empty is retried hourly
PROBE_RETRY = timedelta(minutes=30)  # probes that ran but did not all pass (a failure also pauses, §10.9)
CAP_RECHECK = timedelta(hours=1)  # daily cap without a known next slot
PROBE_ROLES = ("planner", "coder")  # the roles the five probes exercise (§10.9)


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
        self.first_start_done = False  # M11 complete: catalog filled and probes passed
        self.catalog_ok = False
        self.probes_ok = False
        self.catalog_retry_at: datetime | None = None
        self.probes_retry_at: datetime | None = None
        self.alerted: set[str] = set()  # one alert per episode (catalog_empty, model_unavailable)
        self.scout_hold_until: datetime | None = None  # proposal.max_per_day used up
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

    def _alert(self, key: str, text: str) -> None:
        if key not in self.alerted:
            self.alerted.add(key)
            self.sc.say(text)

    def first_start(self) -> bool:
        """M11, in order; True once the catalog has ids and the probes passed for the tuple."""
        if self.first_start_done:
            return True
        now = self.now()
        if not self.catalog_ok:
            if [r for r in self.sc.gk.catalog() if not r.get("forced")]:
                self.catalog_ok = True
            elif self.catalog_retry_at is not None and now < self.catalog_retry_at:
                return False
            elif not self._catalog():
                self.catalog_retry_at = now + CATALOG_RETRY
                self._alert("catalog_empty", "⚠️ catalog_empty: the Kiro model catalog could not be loaded"
                            " (list_models). No Kiro job starts until it loads; retrying hourly.")  # fmt: skip
                return False
            else:
                self.catalog_ok = True
            self.alerted.discard("catalog_empty")
        if not self.probes_ok:
            if self.probes_retry_at is not None and now < self.probes_retry_at:
                return False
            self.probes_ok = self._probes()
            if not self.probes_ok:
                self.probes_retry_at = now + PROBE_RETRY
                return False
        self.first_start_done = self.catalog_ok = self.probes_ok = True
        return True

    def _probes(self) -> bool:
        try:
            if self.sc.sbx.probes_status().get("all_passed"):
                return True
            cat = Catalog.from_rows(self.sc.gk.catalog())
            models = {role: self.sc.models.resolve(role, cat).model for role in PROBE_ROLES}
            log.info("running the safety probes", extra={"event": "probes_run"})
            self.sc.sbx.probes_run(models)
            passed = bool(self.sc.sbx.probes_status().get("all_passed"))
        except ModelUnavailable as exc:
            self._alert("model_unavailable", f"⚠️ model_unavailable: no catalog model for the probes ({exc}).")
            return False
        except (SbxError, GkError) as exc:
            log.warning("safety probes unavailable: %s", exc, extra={"event": "probes_unavailable"})
            return False
        if not passed:
            log.warning("safety probes did not all pass; Kiro jobs wait", extra={"event": "probes_failed"})
        else:
            self.alerted.discard("model_unavailable")
        return passed

    def _catalog(self) -> bool:
        try:
            catalog.run(self.sc)
        except (StageFailed, SbxError, GkError, Interrupted, Deferred) as exc:
            log.warning("catalog job failed: %s", exc, extra={"event": "catalog_failed"})
            self.last_catalog = self.now()
            return False
        self.last_catalog = self.now()
        return True

    # ------------------------------------------------------------------- tick
    def tick(self) -> Tick:
        out = Tick()
        if not self.sc.gate.open("discover"):
            out.skipped = "stopped"  # killed, paused, draining or down: nothing new starts
            return out
        bundle = self.sc.config()
        if self.first_start_done:
            refresh_h = self.sc.models.get().catalog.refresh_hours
            stale = self.last_catalog is None or self.now() - self.last_catalog > timedelta(hours=refresh_h)
            try:
                claimed = self.sc.gk.claim_refresh()
            except GkError:
                claimed = False
            if claimed or stale:
                self._catalog()  # on failure the previous catalog stays in effect (§20)
        if not self.first_start():
            out.skipped = "not_ready"  # M11: no discovery, no stage before catalog + probes
            return out
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

    def _eligible(self, c: dict[str, Any], stage: object) -> bool:
        state = str(c.get("state"))
        if stage is scout.run and self.scout_hold_until is not None and c.get("source") != "user" \
                and c.get("proposal_version") is None:  # fmt: skip
            if self.now() < self.scout_hold_until:
                return False  # max_per_day used up: no autonomous first scout until the next slot
            self.scout_hold_until = None
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
            if not self.first_start_done:
                break  # a probe-gate refusal during this tick: hold the rest
            cid, state = str(c["id"]), str(c.get("state"))
            if state in TERMINAL or cid in self.running:
                continue
            stage = stages.stage_for(c)
            if stage is None or not self._eligible(c, stage):
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
        except ProbesNotReady:
            log.warning("probe gate refused a Kiro job for %s; holding stages", cid,
                        extra={"event": "probe_gate", "cid": cid})  # fmt: skip
            self.probes_ok = False  # the next tick re-checks and runs the probes (M11 step 3)
            self.probes_retry_at = None
            self.first_start_done = False
        except CapReached as exc:
            self.scout_hold_until = exc.until or self.now() + CAP_RECHECK
            log.info("proposal.max_per_day reached; autonomous scouting waits until %s", self.scout_hold_until,
                     extra={"event": "scout_hold", "cid": cid})  # fmt: skip
        except (Interrupted, Deferred) as exc:
            log.info("stage stopped for %s: %s", cid, exc, extra={"event": "stage_stopped", "cid": cid})
        except StageFailed as exc:
            self.fail(sc, cid, exc.error_class)
        except GkError as exc:
            if exc.status == 409 and exc.code in {"stale_from", "stage_running"}:
                return  # someone else moved it; the next tick re-reads
            if exc.status in {423, 503}:
                return  # stopped meanwhile, or the gatekeeper is restarting
            log.warning("gatekeeper refused %s: %s", cid, exc.code, extra={"event": "stage_refused", "cid": cid})
            self.fail(sc, cid, f"gk_{exc.code}")
        except SbxError as exc:  # sandboxd refusals outside a job (destroy, health); jobs map in errors.py
            if exc.status in {423, 503}:
                return
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
