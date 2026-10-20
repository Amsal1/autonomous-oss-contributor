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
in a gate-owned state (a re-scout from PROPOSED or RESCOPE_REQUIRED) the item keeps its state and
the stage is retried after 10 min, doubling up to 6 h, with one owner message, for as long as the
item stays in that state (the owner's next decision is dispatched at once). GitHub reads retry
network errors and 5xx themselves (``github_read``, then ``github_unavailable``); a rate limit
delays the stage, or the discovery cycle, until the reset (§20). Discovery runs on the tick before
dispatch, so ``github_unavailable`` ends the cycle (not just the target) and the cycle runs again
after ``DISCOVER_UNAVAILABLE_RETRY``: an outage costs one read's retries, not one per target. Interruptions
(control state, a sandbox job cut by a drain or crash, sandboxd unreachable) only end the stage
run as ``interrupted`` and the next tick re-queues it (max 3 by the gatekeeper). On start, stage
runs left ``running`` by a crashed runner become ``interrupted``; ``resumed`` counts them
(``ossctl up`` reports it).
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
from ossbot.common.models import LIVE_TEST_ROLE, Catalog, ModelUnavailable
from ossbot.runner import stages
from ossbot.runner.errors import CapReached, Deferred, Interrupted, ProbesNotReady, StageFailed
from ossbot.runner.github_read import GitHubRateLimited
from ossbot.runner.gk_client import GkError
from ossbot.runner.pipeline import StageContext
from ossbot.runner.sbx_client import SbxError
from ossbot.runner.stages import catalog, common, discover, scout

log = logging.getLogger(__name__)
TERMINAL = frozenset({"SKIPPED", "ABANDONED", "REJECTED", "EXPIRED", "FAILED", "DRY_RUN_DONE", "MERGED", "CLOSED",
                      "PROPOSAL_REJECTED"})  # fmt: skip
NO_FAIL = frozenset({"PROPOSED", "RESCOPE_REQUIRED", "AWAITING_APPROVAL", "APPROVED", "SUBMITTING", "SUBMITTED",
                     "SUBMIT_FAILED"})  # fmt: skip  # G/H-owned: a stage error never fails them from the runner
# Before Start work: a task volume holds only the scout clone (sandboxd.jobs.PRE_START_STATES)
PRE_START = frozenset({"DISCOVERED", "VETTING", "VETTED", "SCOUTING", "PROPOSED", "PROPOSAL_SNOOZED",
                       "PROPOSAL_EXPIRED"})  # fmt: skip
CATALOG_RETRY = timedelta(hours=1)  # §20: catalog_empty is retried hourly
PROBE_RETRY = timedelta(minutes=30)  # probes that ran but did not all pass (a failure also pauses, §10.9)
CAP_RECHECK = timedelta(hours=1)  # daily cap without a known next slot
PROBE_ROLES = ("planner", "coder")  # the agents the five probes exercise (§10.9)
GATE_1 = frozenset({"PROPOSED", "RESCOPE_REQUIRED"})  # NO_FAIL states with a stage: the re-scout
HELD_RETRY = timedelta(minutes=10)  # first retry of a stage that failed in a NO_FAIL state (re-scout)
HELD_RETRY_MAX = timedelta(hours=6)  # doubling up to this
DISCOVER_UNAVAILABLE_RETRY = timedelta(minutes=15)  # a cycle ended by github_unavailable runs again after this


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
        self.swept_at_start = False
        self.last_discovery: datetime | None = None
        self.last_catalog: datetime | None = None
        self.refused: set[str] = set()
        # cids a full cycle created so far; a cycle cut short (stop, rate limit, GitHub outage) re-runs
        # against what is left of discovery.proposals_per_cycle, not a fresh budget (DESIGN 9.5b)
        self.cycle_created: list[str] = []
        self.cycle_cut = False
        self.resumed = 0
        # cid -> (state, failures, next try): a stage error in a NO_FAIL state cannot fail the item,
        # so it is retried with backoff and the owner is told once instead of a silent retry every
        # tick; the entry holds only that state (a decision that moves the item drops it)
        self.held: dict[str, tuple[str, int, datetime]] = {}
        # cid -> (state, until): the stage met GitHub's rate limit, retried after the reset (§20)
        self.delayed: dict[str, tuple[str, datetime]] = {}
        self.discover_after: datetime | None = None  # rate limit or github_unavailable: no cycle before
        self.pending_discover: list[str] | None = None  # an ossctl discover request a stop cut short

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
            test_model = self.sc.models.resolve(LIVE_TEST_ROLE, cat).model  # the probes test enforcement
            models = dict.fromkeys(PROBE_ROLES, test_model)
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
    def sweep_volumes(self) -> int:
        """Destroy task volumes and per-task networks nothing needs any more (every cid that ran a
        sandbox job is noted): terminal items (§9.12) and items that have not
        passed Start work (only a scout clone left behind by a kill, crash or drain, §9.5a). Runs on
        every tick, also while stopped (cleanup only, nothing new starts). Returns the count."""
        if not self.swept_at_start:  # once per process: leftovers of a crash or an older runner
            try:
                for c in self.sc.gk.contributions(limit=200):
                    if str(c.get("state")) in TERMINAL:
                        common.note_volume(self.sc, str(c["id"]), True)
                self.swept_at_start = True
            except GkError:
                return 0
        noted = common.volume_cids(self.sc)
        if not noted:
            return 0
        try:  # terminal ones are not listed: they are looked up one by one below
            states = {str(c["id"]): str(c.get("state")) for c in self.sc.gk.active_contributions()}
        except GkError:
            return 0
        done = 0
        for cid in noted:
            if cid in self.running:
                continue
            state = states.get(cid)
            if state is None:
                try:
                    state = str(self.sc.gk.contribution(cid)["state"])
                except GkError as exc:
                    if exc.status == 404:
                        common.note_volume(self.sc, cid, False)
                    continue
            if (state in TERMINAL or state in PRE_START) and common.destroy_volume(self.sc, cid):
                done += 1
        return done

    def tick(self) -> Tick:
        out = Tick()
        self.sweep_volumes()
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
        if self.discover_after is None or self.now() >= self.discover_after:
            self.discover_after = None
            try:
                asked = self.sc.gk.claim_discover()  # ossctl discover [repo...]
            except GkError:
                asked = None
            if asked is None and self.pending_discover is not None:
                asked, self.pending_discover = self.pending_discover, None  # cut short earlier: again
            if asked is not None:
                self.discover(asked or None)
            elif self.last_discovery is None or self.now() - self.last_discovery >= interval:
                self.discover()
        if self.sc.stopping():
            out.skipped = "stopping"  # SIGTERM during discovery: start nothing new
            return out
        self._dispatch(out)
        return out

    def discover(self, repos: list[str] | None = None) -> list[str]:
        if repos is None:
            self.last_discovery = self.now()
            if not self.cycle_cut:  # a cut cycle's re-run keeps its refusals and its spent budget
                self.refused.clear()  # refusals are cached for one discovery cycle
                self.cycle_created = []
            self.cycle_cut = False
        try:
            return discover.run(self.sc, repos=repos, refused=self.refused,
                                created=self.cycle_created if repos is None else None)  # fmt: skip
        except (Interrupted, Deferred) as exc:  # stopped or rate limited mid-cycle: it runs again later
            if repos is None:
                self.last_discovery = None  # the full cycle runs again once open
                self.cycle_cut = True
            else:
                self.pending_discover = repos  # the claimed ossctl discover request is not lost
            if isinstance(exc, GitHubRateLimited):
                self.discover_after = datetime.fromtimestamp(exc.until, UTC)
            elif isinstance(exc, discover.GitHubUnavailable):
                self.discover_after = self.now() + DISCOVER_UNAVAILABLE_RETRY
            log.info("discovery interrupted: %s", exc, extra={"event": "discover_interrupted"})
            return []
        except GkError as exc:
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
        for c in self.sc.gk.active_contributions():  # every non-terminal item, not a window of recent ones
            if not self.first_start_done:
                break  # a probe-gate refusal during this tick: hold the rest
            cid, state = str(c["id"]), str(c.get("state"))
            if state in TERMINAL or cid in self.running:
                continue
            if self._waiting(cid, state):
                continue  # backing off (stage errors in a gate-owned state) or GitHub rate limit
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

    def _waiting(self, cid: str, state: str) -> bool:
        """True while the item waits in the state its hold or delay was recorded for; once the
        state changed (the owner's Continue, a new proposal) the entry is dropped."""
        now = self.now()
        held = self.held.get(cid)
        if held is not None and held[0] != state:
            del self.held[cid]
            held = None
        delayed = self.delayed.get(cid)
        if delayed is not None and (delayed[0] != state or now >= delayed[1]):
            del self.delayed[cid]
            delayed = None
        return delayed is not None or (held is not None and now < held[2])

    # --------------------------------------------------------------- one stage
    def run_one(self, cid: str) -> None:
        sc = dataclasses.replace(self.sc, run=None)  # per-thread stage context
        try:
            c = sc.gk.contribution(cid)
            stage = stages.stage_for(c)
            if stage is None:
                return
            stage(sc, c)
            self.held.pop(cid, None)
            self.delayed.pop(cid, None)
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
        except GitHubRateLimited as exc:  # §20: the stage is delayed until the reset, not failed
            until = datetime.fromtimestamp(exc.until, UTC)
            try:  # the state the stage left it in (vet moves DISCOVERED -> VETTING first)
                self.delayed[cid] = (str(sc.gk.contribution(cid)["state"]), until)
            except GkError:
                self.delayed[cid] = (str(c.get("state")), until)
            log.info("stage delayed for %s until %s: %s", cid, until, exc,
                     extra={"event": "stage_delayed", "cid": cid})  # fmt: skip
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
            self.fail(sc, cid, f"internal:{type(exc).__name__}".lower())

    def fail(self, sc: StageContext, cid: str, error_class: str) -> None:
        try:
            c = sc.gk.contribution(cid)
            state = str(c["state"])
            if state in TERMINAL:
                return
            if state in NO_FAIL:
                self._hold(sc, c, error_class)
                return
            sc.gk.transition(cid, state, "FAILED", {"error_class": error_class[:64]})
            if error_class == "model_unavailable":
                sc.say(f"⚠️ {cid}: no model of its role is in the Kiro catalog (model_unavailable).")
        except GkError as exc:
            log.warning("could not fail %s: %s", cid, exc.code, extra={"event": "fail_refused", "cid": cid})

    def _hold(self, sc: StageContext, c: dict[str, Any], error_class: str) -> None:
        """A gate-owned state (a re-scout from PROPOSED or RESCOPE_REQUIRED) keeps its state; the
        stage is retried after 10 min, doubling up to 6 h, and the owner is told on the first one."""
        cid, state = str(c["id"]), str(c["state"])
        prev = self.held.get(cid)
        count = prev[1] + 1 if prev is not None and prev[0] == state else 1
        delay = min(HELD_RETRY * 2 ** (count - 1), HELD_RETRY_MAX)
        self.held[cid] = (state, count, self.now() + delay)
        log.warning("stage failed for %s in %s (%s); retry #%d in %s", cid, state, error_class, count, delay,
                    extra={"event": "stage_held", "cid": cid})  # fmt: skip
        if count == 1:
            where = f"{c['upstream']}#{c['issue_number']}" if c.get("issue_number") else str(c.get("upstream"))
            if state in GATE_1:
                minutes = int(delay.total_seconds() // 60)
                sc.say(f"⚠️ {where}: revising the proposal failed ({error_class[:64]}). It stays at"
                       f" {state}; retrying in {minutes} min, then less often.")  # fmt: skip
            else:  # a stage error after the item reached gate 2 or submit: the gatekeeper owns it now
                sc.say(f"⚠️ {where}: a runner step failed ({error_class[:64]}) after the item reached {state}."
                       " It stays there.")  # fmt: skip

    def shutdown(self, wait: bool = True) -> None:
        if self.pending_discover is not None:  # kept in memory only: a restart does not run it
            log.warning("ossctl discover request for %s dropped at shutdown; send it again",
                        ", ".join(self.pending_discover), extra={"event": "discover_dropped"})  # fmt: skip
        self.pool.shutdown(wait=wait, cancel_futures=True)
