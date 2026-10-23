"""Job admission and the four-step lifecycle (DESIGN §10.2 M3, §10.4, §10.6, §10.9).

Admission (``submit``): JobSpec rules -> control flags (423) -> model in catalog (400) ->
safety-probe gate for ``entrypoint=kiro`` (423) -> one job per cid (409). Execution: stage-in,
snapshot (research with ``repo_mode=snapshot``), run (the coder is the agent-exec/agent-llm
pair), collect (``/io/output`` archive -> key scan -> artifacts). Containers and the per-run
volumes are always removed; the task volume lives until the contribution is terminal.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ossbot.common.clock import Clock, to_iso
from ossbot.common.control_flags import Blocked, NotUp, read_flags, require_running, require_up
from ossbot.common.images import ImagePins
from ossbot.common.schemas import HOLD_HOST_RE, HOLD_TOP_HOSTS_MAX, TERMINAL_STATES, HoldRequest
from ossbot.sandboxd.argv import Volumes, job_spec, probe_spec, stagein_spec
from ossbot.sandboxd.db import Store
from ossbot.sandboxd.egress_meter import EgressMeter
from ossbot.sandboxd.keyscan import BundleFormatError, KeyScanner
from ossbot.sandboxd.models import CatalogCache, UnknownModel
from ossbot.sandboxd.networks import Networks
from ossbot.sandboxd.probes import (
    FAIL,
    INCONCLUSIVE,
    INCONCLUSIVE_RETRIES,
    MODEL_PROBES,
    PASS,
    PROBES,
    ProbeEvidence,
    ProbeGateError,
    ProbeKey,
    ProbeStore,
    TrustMode,
    Verdict,
    bounded,
    classify_coder_surface,
    classify_research_deny,
    classify_symlink,
    compute_key,
    eval_settings,
    eval_workspace,
    evidence_digest,
)
from ossbot.sandboxd.profiles import (
    CATALOG_CID,
    EGRESS_PROFILES,
    TEMPLATES,
    TRUST_TOOLS,
    FetchArgs,
    JobEgress,
    JobSpec,
    KiroArgs,
    PackageArgs,
    RunCodeArgs,
    SpecError,
    UnbundleArgs,
    ValidateArgs,
    ValidatedJob,
    cid_slug,
    network_profile,
    validate_job,
)
from ossbot.sandboxd.runtime.base import ContainerRuntime, Mount, RuntimeFailure
from ossbot.sandboxd.snapshot import SnapshotError, take_snapshot
from ossbot.sandboxd.stagein import StageError, build_stagein_tar, extract_outputs

log = logging.getLogger(__name__)
EXEC_READY_TIMEOUT_S = 30.0
EXEC_READY_POLL_S = 0.25
PROBE_CID = "sys-probe"
# Per Kiro probe session (owner decision 2026-10-19, F006-R2-7): a normal session takes about 12 s
# and a full run about 80 s; a session cut here is INCONCLUSIVE (probes.bounded), so a hung session
# cannot hold the gate (and every Kiro job behind it) for the old 900 s.
PROBE_SESSION_TIMEOUT_S = 30
INTERRUPTED = "interrupted"  # job error_class after a drain timeout or a crash (CR-001); state 'failed'
INTERRUPT_GRACE_S = 15.0  # after killing the containers, how long the job threads get to record
PROMPT_DIR_PROBES = "/opt/ossbot/probes"
# Before Start work (CR-004 gate 1) a task volume holds only the scout clone and may be destroyed.
PRE_START_STATES = frozenset({"DISCOVERED", "VETTING", "VETTED", "SCOUTING", "PROPOSED", "PROPOSAL_SNOOZED",
                              "PROPOSAL_EXPIRED", "PROPOSAL_REJECTED"})  # fmt: skip
# open-discovery design §13.2: the meter stopped the job / a hold for the cid isn't recorded yet
EGRESS_ANOMALY = "egress_anomaly"
EGRESS_ANOMALY_PENDING = "egress_anomaly_pending"


def hold_body(doc: dict[str, Any]) -> dict[str, Any]:
    """The gatekeeper's ``HoldRequest`` from a meter summary; any host the schema can't carry becomes ``?``."""
    hosts = [h if re.fullmatch(HOLD_HOST_RE, h) else "?" for h in doc.get("top_hosts", [])][:HOLD_TOP_HOSTS_MAX]
    return HoldRequest.model_validate({
        "reason": "egress_anomaly", "run_id": doc["run_id"], "stage": doc["stage"], "egress": doc["egress"],
        "egress_mode": doc["egress_mode"], "hosts": doc["hosts"], "up_bytes": doc["up_bytes"],
        "down_bytes": doc["down_bytes"], "denied": doc["denied"], "top_hosts": hosts, "limits": doc["limits"],
        "breached": doc["breached"],
    }).model_dump(mode="json")  # fmt: skip


class ApiError(Exception):
    def __init__(self, status: int, error_class: str, detail: str = "") -> None:
        super().__init__(f"{status} {error_class}: {detail}")
        self.status = status
        self.error_class = error_class
        self.detail = detail


class HoldError(Exception):
    """The gatekeeper did not record a hold: ``status`` None = transport error (sandboxd retries those and
    5xx; a 4xx such as 400 ``schema_error`` or 404 ``unknown_cid`` is final, review-3 M3)."""

    def __init__(self, status: int | None, code: str) -> None:
        super().__init__(f"{status} {code}")
        self.status = status
        self.code = code


class Gatekeeper(Protocol):
    """The calls sandboxd makes as the ``sandbox`` role (ossbox) on the gatekeeper UDS."""

    def catalog(self) -> list[dict[str, Any]]: ...
    def pause(self, reason: str) -> None: ...
    def contribution_state(self, cid: str) -> str | None: ...
    def probe_result(self, probe_key: str, probe: str, passed: bool, evidence_sha256: str, *,
                     verdict: str | None = None, attempts: int = 1) -> None: ...  # fmt: skip
    def hold(self, cid: str, body: dict[str, Any]) -> None: ...  # raises HoldError (open-discovery §13.3)


@dataclass(frozen=True)
class Settings:
    artifacts_dir: Path
    logs_dir: Path
    staging_dir: Path
    control_dir: Path
    input_roots: tuple[Path, ...]


@dataclass(frozen=True)
class Plan:
    """One execution, derived from a validated JobSpec or built internally by the probe runner."""

    run_id: str
    cid: str
    template: str  # TEMPLATES key, or "coder" for the agent-exec/agent-llm pair
    entrypoint: str
    repo_mode: str
    env: dict[str, str]
    inputs: dict[str, Path]
    timeout_s: int
    cpus: float
    memory_mb: int
    dest: Path
    create_task_volume: bool = False
    release_network: bool = False
    exec_env: dict[str, str] = field(default_factory=dict)
    egress: JobEgress | None = None  # fetch/validate only: "strict" -> the <net>-strict network profile


@dataclass
class Outcome:
    exit_code: int | None = None
    error_class: str | None = None
    files: dict[str, bytes] = field(default_factory=dict)
    logs: bytes = b""

    @property
    def ok(self) -> bool:
        return self.error_class is None and self.exit_code == 0


def _env_for(job: ValidatedJob, trust_mode: TrustMode) -> dict[str, str]:
    a = job.args
    if isinstance(a, FetchArgs):
        env = {"OSSBOT_ARG_REPO": a.repo, "OSSBOT_ARG_CLONE_DEPTH": str(a.clone_depth)}
        if not a.prefetch:  # scout clone (CR-004 §9.5a); absent = the default, so Phase 1 argv is unchanged
            env["OSSBOT_ARG_PREFETCH"] = "0"
        if a.refetch:  # DESIGN §9.8 re-fetch; absent otherwise, so every other fetch argv is unchanged
            env["OSSBOT_ARG_REFETCH"] = "1"
        if a.base_ref is not None:
            env["OSSBOT_ARG_BASE_REF"] = a.base_ref
        if a.base_sha is not None:
            env["OSSBOT_ARG_BASE_SHA"] = a.base_sha
        return env
    if isinstance(a, KiroArgs):
        env = {
            "ROLE": a.role, "PROMPT_FILE": a.prompt_file, "MODEL": a.model, "EFFORT": a.effort,
            "TRUST_TOOLS": TRUST_TOOLS[a.role], "OSSBOT_TRUST_MODE": trust_mode,
        }  # fmt: skip
        if a.role == "coder":
            env["REQUIRE_MCP"] = "1"
        return env
    if isinstance(a, ValidateArgs):
        return {"OSSBOT_ARG_PLAN_FILE": a.plan_file}
    if isinstance(a, PackageArgs):
        return {
            "OSSBOT_ARG_ROUND_BASE": a.round_base, "OSSBOT_ARG_PACKAGE_TS": a.package_ts,
            "OSSBOT_ARG_NAME": a.name, "OSSBOT_ARG_EMAIL": a.email,
        }  # fmt: skip
    if isinstance(a, UnbundleArgs):
        return {
            "OSSBOT_ARG_MODE": a.mode, "OSSBOT_ARG_BUNDLE_REF": a.bundle_ref,
            "OSSBOT_ARG_CHECKOUT_SHA": a.checkout_sha, "OSSBOT_ARG_REV": str(a.rev),
        }  # fmt: skip
    if isinstance(a, RunCodeArgs):
        return {"OSSBOT_ARG_LANGUAGE": a.language, "OSSBOT_ARG_TIMEOUT_S": str(a.timeout_s)}
    return {}


class JobService:
    def __init__(
        self,
        store: Store,
        runtime: ContainerRuntime,
        pins: Callable[[], ImagePins],
        catalog: CatalogCache,
        gatekeeper: Gatekeeper,
        scanner: KeyScanner,
        clock: Clock,
        settings: Settings,
        trust_mode: Callable[[], TrustMode],
        networks: Networks,
        background: bool = True,
        meter: EgressMeter | None = None,
        egress_mode: Callable[[], str] = lambda: "open",
    ) -> None:
        self.meter = meter  # open-discovery §13.2: fetch/validate jobs are metered (None: not metered)
        self.egress_mode = egress_mode
        self._anomaly: dict[str, dict[str, Any]] = {}  # run_id -> the meter summary that stopped it
        self.store = store
        self.runtime = runtime
        self.pins = pins
        self.catalog = catalog
        self.gatekeeper = gatekeeper
        self.scanner = scanner
        self.clock = clock
        self.settings = settings
        self.trust_mode = trust_mode
        self.networks = networks
        self.probes = ProbeStore(store, clock)
        self.background = background
        self._active: dict[str, str] = {}  # cid -> run_id
        self._containers: dict[str, list[str]] = {}  # run_id -> container names
        self._cancelled: set[str] = set()
        self._interrupted: set[str] = set()
        self._running: set[str] = set()  # submitted jobs whose final row is not written yet
        self._draining = False
        self._idle = threading.Condition()
        self._lock = threading.Lock()

    # --------------------------------------------------------------------- admission
    def probe_key(self) -> ProbeKey | None:
        return compute_key(self.pins(), self.trust_mode())

    def admit(self, spec: JobSpec) -> Plan:
        try:
            job = validate_job(spec)
        except SpecError as exc:
            raise ApiError(400, exc.code, str(exc)) from exc
        try:
            require_running(self.settings.control_dir, f"sandbox_job:{spec.entrypoint}")
        except Blocked as exc:
            raise ApiError(423, exc.flags.state, str(exc)) from exc
        try:  # CR-001: draining/down admits no new jobs; running ones may finish
            require_up(self.settings.control_dir, f"sandbox_job:{spec.entrypoint}")
        except NotUp as exc:
            raise ApiError(423, exc.info.state, str(exc)) from exc
        if self._draining:
            raise ApiError(423, "draining", "sandboxd is draining")
        if spec.profile in EGRESS_PROFILES and self.pending_hold(spec.cid) is not None:
            # §13.2 fail closed: the gatekeeper hasn't recorded the hold (yet, or it refused it)
            raise ApiError(423, EGRESS_ANOMALY_PENDING, f"an egress anomaly of {spec.cid} awaits its hold")
        trust_mode = self.trust_mode()
        if isinstance(job.args, KiroArgs):
            try:
                self.catalog.validate(job.args.model)
            except UnknownModel as exc:
                raise ApiError(400, "unknown_model", str(exc)) from exc
            try:
                self.probes.gate(self.probe_key())
            except ProbeGateError as exc:
                raise ApiError(423, exc.error_class, str(exc)) from exc
        for name, raw in spec.inputs.items():
            path = Path(raw)
            if not path.is_absolute() or not any(path == r or r in path.parents for r in self.settings.input_roots):
                raise ApiError(400, "bad_input", f"input {name} outside the allowed roots")
        if isinstance(job.args, PackageArgs) and spec.entrypoint == "package":
            dest = self.settings.artifacts_dir / spec.cid / str(job.args.rev)
        else:
            dest = self.settings.artifacts_dir / spec.cid / "runs" / spec.run_id
        template = "coder" if spec.profile == "coder" else spec.profile
        refetch = isinstance(job.args, FetchArgs) and job.args.refetch  # §9.8: into the existing task volume
        return Plan(
            run_id=spec.run_id, cid=spec.cid, template=template, entrypoint=spec.entrypoint,
            repo_mode=spec.repo_mode, env=_env_for(job, trust_mode),
            inputs={k: Path(v) for k, v in spec.inputs.items()}, timeout_s=spec.timeout_s, cpus=spec.cpus,
            memory_mb=spec.memory_mb, dest=dest,
            create_task_volume=spec.entrypoint == "fetch" and not refetch, release_network=spec.cid == CATALOG_CID,
            egress=spec.egress,
        )  # fmt: skip

    def submit(self, spec: JobSpec) -> dict[str, Any]:
        plan = self.admit(spec)
        with self._lock:
            if plan.cid in self._active:
                raise ApiError(409, "cid_busy", f"job {self._active[plan.cid]} is running for {plan.cid}")
            with self.store.tx() as conn:
                if conn.execute("SELECT 1 FROM jobs WHERE run_id = ?", (plan.run_id,)).fetchone():
                    raise ApiError(409, "run_id_exists")
                conn.execute(
                    "INSERT INTO jobs (run_id, cid, profile, entrypoint, state, created_at) VALUES (?,?,?,?,?,?)",
                    (plan.run_id, plan.cid, spec.profile, spec.entrypoint, "queued", to_iso(self.clock.now())),
                )
            self._active[plan.cid] = plan.run_id
        with self._idle:
            self._running.add(plan.run_id)
        if self.background:
            threading.Thread(target=self._run_job, args=(plan,), daemon=True, name=f"job-{plan.run_id}").start()
        else:
            self._run_job(plan)
        return self.get(plan.run_id)

    def _run_job(self, plan: Plan) -> None:
        try:
            self._set_state(plan.run_id, "running")
            try:
                outcome = self.execute(plan)
            except Exception as exc:
                log.exception("job crashed", extra={"run_id": plan.run_id, "cid": plan.cid})
                outcome = Outcome(error_class=f"internal:{type(exc).__name__}")
            finally:
                with self._lock:
                    self._active.pop(plan.cid, None)
            self._record(plan, outcome)
        finally:
            with self._idle:  # the drain waits on this (durable row first, then "not running")
                self._running.discard(plan.run_id)
                self._idle.notify_all()

    def _record(self, plan: Plan, outcome: Outcome) -> None:
        state = "cancelled" if plan.run_id in self._cancelled else ("succeeded" if outcome.ok else "failed")
        if plan.run_id in self._interrupted:  # CR-001 drain timeout: the runner re-queues the stage
            state, outcome.error_class, outcome.exit_code = "failed", INTERRUPTED, None
        if self._anomaly.pop(plan.run_id, None) is not None:  # §13.2: stopped by the egress meter
            state, outcome.error_class = "failed", EGRESS_ANOMALY
        with self.store.tx() as conn:
            conn.execute(
                "UPDATE jobs SET state = ?, exit_code = ?, error_class = ?, artifact_dir = ?, finished_at = ?"
                " WHERE run_id = ?",
                (state, outcome.exit_code, outcome.error_class, str(plan.dest) if outcome.ok else None,
                 to_iso(self.clock.now()), plan.run_id),
            )  # fmt: skip

    def _set_state(self, run_id: str, state: str) -> None:
        with self.store.tx() as conn:
            conn.execute("UPDATE jobs SET state = ? WHERE run_id = ?", (state, run_id))

    def get(self, run_id: str) -> dict[str, Any]:
        with self.store.lock:
            row = self.store.conn.execute("SELECT * FROM jobs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise ApiError(404, "not_found")
        return dict(row)

    # --------------------------------------------------------------------- execution
    def _image(self, image: str) -> str:
        ref = self.pins().ref(image)
        if ref is None:
            raise RuntimeFailure(f"image {image} has no pinned digest")
        return ref

    def _track(self, run_id: str, name: str) -> None:
        with self._lock:
            self._containers.setdefault(run_id, []).append(name)

    def execute(self, plan: Plan) -> Outcome:
        vols = Volumes(plan.run_id, plan.cid)
        created: list[str] = []
        volumes: list[str] = []
        outcome = Outcome()
        try:
            full = self._image("sandbox-full")
            require_running(self.settings.control_dir, f"sandbox_job:{plan.entrypoint}")
            labels = {"ossbot.sandbox": "1", "ossbot.cid": cid_slug(plan.cid), "ossbot.run": plan.run_id}
            for name in (vols.input, vols.output):
                self.runtime.create_volume(name, labels)
                volumes.append(name)
            if plan.create_task_volume:
                self.runtime.create_volume(vols.task, {"ossbot.sandbox": "1", "ossbot.cid": cid_slug(plan.cid)})
            self._stage_in(plan, full, created)
            if plan.repo_mode == "snapshot":
                self.runtime.create_volume(vols.ro, labels)
                volumes.append(vols.ro)
                take_snapshot(self.runtime, plan.run_id, plan.cid, full, created)
            mains = self._run_main(plan, vols, labels, volumes, created)
            outcome = self._collect(plan, mains)
        except Blocked as exc:
            outcome.error_class = exc.flags.state
        except SnapshotError as exc:
            outcome.error_class = exc.error_class
            if exc.error_class == "snapshot_unsafe":
                self._pause("snapshot_unsafe")
        except (StageError, RuntimeFailure) as exc:
            log.warning("job failed: %s", exc, extra={"run_id": plan.run_id, "cid": plan.cid})
            if isinstance(exc, StageError):
                outcome.error_class = "stage_in"
            else:
                outcome.error_class = "timeout" if str(exc) == "timeout" else "runtime"
        finally:
            for name in reversed(created):
                self._quiet(self.runtime.remove, name)
            for name in volumes:
                self._quiet(self.runtime.remove_volume, name)
            if plan.release_network:
                self.networks.release(plan.cid)
            with self._lock:
                self._containers.pop(plan.run_id, None)
        return outcome

    def _quiet(self, fn: Callable[[str], None], name: str) -> None:
        try:
            fn(name)
        except RuntimeFailure as exc:
            log.warning("cleanup of %s failed: %s", name, exc)

    def _pause(self, reason: str) -> None:
        try:
            self.gatekeeper.pause(reason)
        except Exception:
            log.exception("could not pause the pipeline (%s)", reason)

    def _stage_in(self, plan: Plan, full: str, created: list[str]) -> None:
        tar = build_stagein_tar(plan.inputs, self.settings.input_roots) if plan.inputs else b""
        spec = stagein_spec(plan.run_id, plan.cid, full)
        container = self.runtime.run(spec)
        created.append(spec.name)
        if tar:
            self.runtime.put_archive(container, "/in", tar)
        self.runtime.remove(spec.name)
        created.remove(spec.name)

    def _network(self, template: str, cid: str, egress: JobEgress | None = None) -> str:
        net = TEMPLATES[template].network
        return "none" if net == "none" else self.networks.ensure(cid, network_profile(net, egress))

    def _run_main(
        self, plan: Plan, vols: Volumes, labels: dict[str, str], volumes: list[str], created: list[str]
    ) -> list[tuple[str, str]]:
        """Start the job container(s); returns (container name, output subdir) after they exited."""
        if plan.template == "coder":
            for name in (vols.exec, vols.exec_output):
                self.runtime.create_volume(name, labels)
                volumes.append(name)
            exec_spec = job_spec(
                "agent-exec", run_id=plan.run_id, cid=plan.cid, entrypoint="exec",
                image_ref=self._image("sandbox-full"), network="none", repo_mode="rw", env=plan.exec_env,
                cpus=plan.cpus, memory_mb=plan.memory_mb,
            )  # fmt: skip
            self.runtime.run(exec_spec)
            created.append(exec_spec.name)
            self._track(plan.run_id, exec_spec.name)
            self._wait_running(exec_spec.name)
            llm_spec = job_spec(
                "agent-llm", run_id=plan.run_id, cid=plan.cid, entrypoint=plan.entrypoint,
                image_ref=self._image("sandbox-llm"), network=self._network("agent-llm", plan.cid),
                repo_mode="none", env=plan.env,
            )  # fmt: skip
            self.runtime.run(llm_spec)
            created.append(llm_spec.name)
            self._track(plan.run_id, llm_spec.name)
            self._wait(plan, llm_spec.name)
            self._quiet(self.runtime.kill, exec_spec.name)
            return [(llm_spec.name, ""), (exec_spec.name, "exec")]
        template = TEMPLATES[plan.template]
        network = self._network(plan.template, plan.cid, plan.egress)
        metered = self._meter_start(plan, template.network)
        try:
            spec = job_spec(
                plan.template, run_id=plan.run_id, cid=plan.cid, entrypoint=plan.entrypoint,
                image_ref=self._image(template.image), network=network, repo_mode=plan.repo_mode, env=plan.env,
                cpus=plan.cpus, memory_mb=plan.memory_mb,
            )  # fmt: skip
            self.runtime.run(spec)
            created.append(spec.name)
            self._track(plan.run_id, spec.name)
            self._wait(plan, spec.name)
        finally:
            if metered and self.meter is not None:  # the finish check always runs (timeout, crash)
                self.meter.finish(plan.run_id)
        return [(spec.name, "")]

    def _meter_start(self, plan: Plan, net: str) -> bool:
        """Register a fetch/validate job (mode or strict egress) with the egress meter (§13.2)."""
        if self.meter is None or plan.template not in EGRESS_PROFILES or plan.egress is None or net == "none":
            return False
        subnet = self.networks.subnets.allocate(network_profile(net, plan.egress), plan.cid)  # idempotent
        try:
            mode = self.egress_mode()
        except Exception:  # noqa: BLE001 - the mode is a label of the summary only
            mode = "open"
        self.meter.register(plan.run_id, plan.cid, plan.template, subnet, self.clock.now().timestamp(),
                            plan.egress, mode)  # fmt: skip
        return True

    # ------------------------------------------------------------- egress anomaly (§13.2)
    def flag_anomaly(self, run_id: str, doc: dict[str, Any]) -> None:
        """The meter's breach: stop the job (its outcome becomes ``egress_anomaly``, staging discarded),
        then ask the gatekeeper for the contribution's hold (once per job)."""
        with self._lock:
            if run_id in self._anomaly:
                return
            self._anomaly[run_id] = doc
            names = list(self._containers.get(run_id, []))
        for name in names:
            self._quiet(self.runtime.kill, name)
        try:
            body = hold_body(doc)
        except (KeyError, ValueError) as exc:  # a bug, not the gatekeeper's answer: still fail closed
            self._pending(str(doc["cid"]), run_id, {"summary": doc}, f"local:{type(exc).__name__}")
            return
        self.request_hold(str(doc["cid"]), run_id, body)

    def request_hold(self, cid: str, run_id: str, body: dict[str, Any]) -> bool:
        """True once the gatekeeper recorded it. M3: 5xx and transport errors stay pending and are retried
        every 30 s; any 4xx (400 ``schema_error``, 404 ``unknown_cid``) is final: the row keeps the cid blocked
        with ``rejected_code`` and an ERROR, until ``ossctl egress release``."""
        try:
            self.gatekeeper.hold(cid, body)
        except HoldError as exc:
            final = exc.status is not None and exc.status < 500
            self._pending(cid, run_id, body, f"{exc.status}:{exc.code}"[:80] if final else None)
            return False
        with self.store.tx() as conn:
            conn.execute("DELETE FROM anomaly_pending WHERE cid = ?", (cid,))
        return True

    def _pending(self, cid: str, run_id: str, body: dict[str, Any], rejected: str | None) -> None:
        now = to_iso(self.clock.now())
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO anomaly_pending (cid, run_id, summary_json, created_at, rejected_code, attempts,"
                " last_attempt_at) VALUES (?, ?, ?, ?, ?, 1, ?) ON CONFLICT (cid) DO UPDATE SET rejected_code ="
                " excluded.rejected_code, attempts = attempts + 1, last_attempt_at = excluded.last_attempt_at",
                (cid, run_id, json.dumps(body, sort_keys=True), now, rejected, now),
            )  # fmt: skip
        if rejected is not None:
            log.error("the gatekeeper refused the hold of %s (%s); its fetch/validate jobs stay refused until"
                      " ossctl egress release", cid, rejected,
                      extra={"event": "hold_rejected", "cid": cid, "run_id": run_id})  # fmt: skip
        else:
            log.warning("hold of %s not recorded yet; retried every 30 s", cid,
                        extra={"event": "hold_pending", "cid": cid, "run_id": run_id})  # fmt: skip

    def retry_pending_holds(self) -> int:
        """The meter thread, every 30 s: pending rows without ``rejected_code``; returns how many cleared."""
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT cid, run_id, summary_json FROM anomaly_pending WHERE rejected_code IS NULL ORDER BY created_at"
            ).fetchall()  # fmt: skip
        return sum(self.request_hold(str(r["cid"]), str(r["run_id"]), json.loads(r["summary_json"])) for r in rows)

    def pending_hold(self, cid: str) -> dict[str, Any] | None:
        with self.store.lock:
            row = self.store.conn.execute("SELECT * FROM anomaly_pending WHERE cid = ?", (cid,)).fetchone()
        return None if row is None else dict(row)

    def pending_holds(self) -> list[dict[str, Any]]:
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT cid, run_id, created_at, rejected_code, attempts, last_attempt_at FROM anomaly_pending"
                " ORDER BY created_at"
            ).fetchall()  # fmt: skip
        return [dict(r) for r in rows]

    def clear_pending(self, cid: str) -> None:
        """``ossctl egress release`` (root, TTY-confirmed): the owner lets the cid's jobs run again."""
        with self.store.tx() as conn:
            if conn.execute("DELETE FROM anomaly_pending WHERE cid = ?", (cid,)).rowcount == 0:
                raise ApiError(404, "no_pending_hold")
        log.warning("pending egress hold of %s cleared by the operator", cid,
                    extra={"event": "hold_pending_cleared", "cid": cid})  # fmt: skip

    def _wait_running(self, name: str) -> None:
        deadline = time.monotonic() + EXEC_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            status = self.runtime.inspect_status(name)
            if status == "running":
                return
            if status in {"exited", "dead"}:
                break
            time.sleep(EXEC_READY_POLL_S)
        raise RuntimeFailure("agent-exec did not reach State.Running within 30 s")

    def _wait(self, plan: Plan, name: str) -> None:
        try:
            self.runtime.wait(name, plan.timeout_s)
        except TimeoutError:
            self._quiet(self.runtime.kill, name)
            raise RuntimeFailure("timeout") from None

    def _collect(self, plan: Plan, mains: list[tuple[str, str]]) -> Outcome:
        outcome = Outcome()
        staging = self.settings.staging_dir / plan.run_id
        if staging.exists():
            shutil.rmtree(staging)
        logs = b""
        for name, sub in mains:
            extract_outputs(self.runtime.get_archive(name, "/io/output"), staging / sub if sub else staging)
            logs += self.runtime.logs(name, None)
        code = self._exit_code(mains[0][0])
        outcome.exit_code = code
        files = sorted(p for p in staging.rglob("*") if p.is_file())
        leaked = self.scanner.enabled and self.scanner.hit(logs)
        for path in files:
            if leaked:
                break
            try:
                leaked = self.scanner.scan_bundle(path) if path.suffix == ".bundle" else self.scanner.scan_file(path)
            except BundleFormatError:
                outcome.error_class = "bundle_unreadable"
                leaked = False
                break
        log_dir = self.settings.logs_dir / "runs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"{plan.run_id}.log").write_bytes(self.scanner.redact(logs))
        if leaked:
            shutil.rmtree(staging, ignore_errors=True)
            outcome.error_class = "secret_in_output"
            self._pause("secret in output")
            return outcome
        if plan.run_id in self._anomaly:  # §13.2: nothing a job over its egress limits produced is kept
            shutil.rmtree(staging, ignore_errors=True)
            outcome.error_class = EGRESS_ANOMALY
            return outcome
        if plan.run_id in self._cancelled:
            outcome.error_class = "cancelled"
        if outcome.error_class is not None:
            shutil.rmtree(staging, ignore_errors=True)
            return outcome
        outcome.files = {str(p.relative_to(staging)): p.read_bytes() for p in files if p.stat().st_size < 4 << 20}
        if code != 0:
            outcome.error_class = self._result_error(staging) or f"exit_{code}"
        plan.dest.parent.mkdir(parents=True, exist_ok=True)
        if plan.dest.exists():
            shutil.rmtree(plan.dest)
        shutil.move(str(staging), str(plan.dest))
        self._share(plan.dest)
        outcome.logs = logs
        return outcome

    def _share(self, dest: Path) -> None:
        """Artifacts are read by the runner and the gatekeeper through group ``ossart`` (§7.3): the
        staged tree was created in sandboxd's private staging dir, so its group follows the
        artifacts root (a rename keeps the old group). Files stay 0640, directories 0750."""
        root = self.settings.artifacts_dir
        try:
            gid = root.stat().st_gid
            paths = [p for p in dest.parents if root in p.parents] + [dest, *dest.rglob("*")]
            for path in paths:
                st = path.lstat()
                if st.st_gid != gid and not path.is_symlink():
                    os.chown(path, -1, gid)
        except OSError as exc:
            log.warning("artifacts not shared with group ossart: %s", exc, extra={"event": "artifacts_share"})

    def _exit_code(self, name: str) -> int:
        try:
            return self.runtime.wait(name, 30)
        except TimeoutError:
            return -1

    @staticmethod
    def _result_error(staging: Path) -> str | None:
        try:
            doc = json.loads((staging / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        value = doc.get("error_class") if isinstance(doc, dict) else None
        return value if isinstance(value, str) and len(value) <= 64 else None

    # ----------------------------------------------------------------- cancel / kill
    def cancel(self, run_id: str) -> None:
        self.get(run_id)
        self._cancelled.add(run_id)
        with self._lock:
            names = list(self._containers.get(run_id, []))
        for name in names:
            self._quiet(self.runtime.kill, name)

    def kill_all(self, reason: str) -> int:
        with self._lock:
            names = [n for names in self._containers.values() for n in names]
            self._cancelled.update(self._containers)
        for name in names:
            self._quiet(self.runtime.kill, name)
        if names:
            log.warning("killed %d sandbox containers (%s)", len(names), reason)
        return len(names)

    # ------------------------------------------------------------ drain / crash (CR-001)
    def running(self) -> list[str]:
        with self._idle:
            return sorted(self._running)

    def _wait_idle(self, deadline: float) -> None:
        """Wait (monotonic clock: sleep/hibernate does not count) until no job is running."""
        with self._idle:
            while self._running:
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                self._idle.wait(min(left, 1.0))

    def drain(self, timeout_s: float) -> dict[str, Any]:
        """Let running jobs finish for up to ``timeout_s``; interrupt the rest.

        New jobs are already refused (the lifecycle flag, or a kill). Interrupted jobs end
        ``failed``/``interrupted`` with no artifacts; their task volume stays, so the runner
        can restart the stage from its last completed one on the next ``up``.
        """
        self._draining = True  # also closes admission if the lifecycle flag was not written
        try:
            return self._drain(timeout_s)
        finally:
            self._draining = False

    def _drain(self, timeout_s: float) -> dict[str, Any]:
        before = set(self.running())
        killed = read_flags(self.settings.control_dir).killed is not None
        if not killed:
            self._wait_idle(time.monotonic() + max(0.0, timeout_s))
        left = set(self.running())
        if left:
            with self._lock:
                self._interrupted.update(left)
                self._cancelled.update(left)
                names = [n for run_id in left for n in self._containers.get(run_id, [])]
            for name in names:
                self._quiet(self.runtime.kill, name)
            log.warning("drain: interrupting %d job(s)", len(left), extra={"run_ids": sorted(left)})
            self._wait_idle(time.monotonic() + INTERRUPT_GRACE_S)
        still = set(self.running())
        if still:  # threads that did not record in time: write the row ourselves
            self._mark_interrupted(still)
        return {
            "finished": len(before - left),
            "interrupted": len(left),
            "interrupted_run_ids": sorted(left),
            "killed": killed,
        }

    def _mark_interrupted(self, run_ids: set[str]) -> None:
        with self.store.tx() as conn:
            for run_id in sorted(run_ids):
                conn.execute(
                    "UPDATE jobs SET state = 'failed', error_class = ?, exit_code = NULL, artifact_dir = NULL,"
                    " finished_at = ? WHERE run_id = ? AND state IN ('queued','running')",
                    (INTERRUPTED, to_iso(self.clock.now()), run_id),
                )

    def recover(self, sweep_orphans: bool = True) -> dict[str, Any]:
        """At start: rows a dead sandboxd left queued/running become ``interrupted`` (durable
        first), then their containers and per-run volumes are removed. ``sweep_orphans`` also
        removes every other ``ossbot.sandbox=1`` container (probe/fixture runs have no row);
        nothing of a previous process can still be wanted. Task volumes are kept (re-queue).
        """
        with self.store.tx() as conn:
            rows = conn.execute("SELECT run_id, cid FROM jobs WHERE state IN ('queued','running')").fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE jobs SET state = 'failed', error_class = ?, exit_code = NULL, artifact_dir = NULL,"
                    " finished_at = ? WHERE run_id = ?",
                    (INTERRUPTED, to_iso(self.clock.now()), row["run_id"]),
                )
        run_ids = {str(r["run_id"]) for r in rows}
        removed = 0
        try:
            containers = self.runtime.list({"ossbot.sandbox": "1"})
        except RuntimeFailure as exc:
            log.warning("recover: cannot list sandbox containers: %s", exc)
            containers = []
        for info in containers:
            if sweep_orphans or info.labels.get("ossbot.run") in run_ids:
                self._quiet(self.runtime.remove, info.name)
                removed += 1
        for row in rows:
            vols = Volumes(str(row["run_id"]), str(row["cid"]))
            for name in (vols.input, vols.output, vols.ro, vols.exec, vols.exec_output):
                self._quiet(self.runtime.remove_volume, name)
        if rows or removed:
            log.warning(
                "recovered after an unclean stop: %d job(s) interrupted, %d container(s) removed", len(rows), removed,
                extra={"event": "sandboxd_recovered", "run_ids": sorted(run_ids)},
            )  # fmt: skip
        return {"interrupted": sorted(run_ids), "containers_removed": removed}

    def logs(self, run_id: str, tail: int | None) -> bytes:
        self.get(run_id)
        path = self.settings.logs_dir / "runs" / f"{run_id}.log"
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return b""
        if tail is not None:
            data = b"\n".join(data.splitlines()[-tail:])
        return data

    def destroy_task(self, cid: str) -> None:
        """Task volume, networks and subnets of a terminal contribution (§9.12), or of one that has
        not passed Start work: before gate 1 the volume holds only the scout clone (§9.5a)."""
        state = self.gatekeeper.contribution_state(cid)
        if state is None:
            raise ApiError(404, "not_found")
        if state not in {s.value for s in TERMINAL_STATES} | PRE_START_STATES:
            raise ApiError(409, "not_terminal", f"contribution is {state}")
        with self._lock:
            if cid in self._active:
                raise ApiError(409, "cid_busy")
        self.networks.release(cid)
        self._quiet(self.runtime.remove_volume, Volumes("x", cid).task)


class ProbeRunner:
    """``POST /v1/probes/run``: the five probes against the baked fixture (internal jobs only)."""

    def __init__(self, jobs: JobService, proxy_log: Callable[[], str] | None = None) -> None:
        self.jobs = jobs
        self.proxy_log = proxy_log or (lambda: "")

    def _plan(self, name: str, template: str, entrypoint: str, repo_mode: str, env: dict[str, str]) -> Plan:
        run_id = f"probe-{name.replace('_', '-')}-{int(time.time())}"
        timeout = PROBE_SESSION_TIMEOUT_S if entrypoint == "kiro" else 900
        return Plan(
            run_id=run_id, cid=PROBE_CID, template=template, entrypoint=entrypoint, repo_mode=repo_mode, env=env,
            inputs={}, timeout_s=timeout, cpus=2.0, memory_mb=2048,
            dest=self.jobs.settings.artifacts_dir / PROBE_CID / "runs" / run_id,
        )  # fmt: skip

    def _kiro_env(self, role: str, prompt: str, model: str, trust_mode: TrustMode) -> dict[str, str]:
        env = {
            "ROLE": role, "PROMPT_FILE": f"{prompt}.md", "MODEL": model, "EFFORT": "low",
            "TRUST_TOOLS": TRUST_TOOLS[role], "OSSBOT_TRUST_MODE": trust_mode, "OSSBOT_PROMPT_DIR": PROMPT_DIR_PROBES,
        }  # fmt: skip
        if role == "coder":
            env["REQUIRE_MCP"] = "1"
        return env

    def _fixture(self) -> None:
        """Fresh probe task volume with the fixture repo untarred (symlinks preserved)."""
        rt, run_id = self.jobs.runtime, f"probe-fixture-{int(time.time())}"
        task = Volumes(run_id, PROBE_CID).task
        self.jobs._quiet(rt.remove_volume, task)
        rt.create_volume(task, {"ossbot.sandbox": "1", "ossbot.cid": PROBE_CID})
        spec = probe_spec(run_id, PROBE_CID, self.jobs._image("sandbox-full"), "extract_fixture",
                          (Mount(task, "/work"),), suffix="fixture")  # fmt: skip
        rt.run(spec)
        try:
            if rt.wait(spec.name, 300) != 0:
                raise RuntimeFailure("fixture extraction failed")
        finally:
            self.jobs._quiet(rt.remove, spec.name)

    def _run(self, plan: Plan) -> ProbeEvidence:
        outcome = self.jobs.execute(plan)
        return ProbeEvidence(files=outcome.files, exit_code=outcome.exit_code if outcome.exit_code is not None else -1,
                             timed_out=outcome.error_class == "timeout")  # fmt: skip

    @staticmethod
    def _retry(
        run: Callable[[], ProbeEvidence], classify: Callable[[ProbeEvidence], Verdict]
    ) -> tuple[ProbeEvidence, Verdict, int]:
        """A model-dependent probe: an INCONCLUSIVE run (refusal, no tool call, a session cut at
        ``PROBE_SESSION_TIMEOUT_S``) is retried with a fresh session up to ``INCONCLUSIVE_RETRIES``
        times; PASS or FAIL ends it at once."""
        evidence, verdict = run(), INCONCLUSIVE
        for attempt in range(1, INCONCLUSIVE_RETRIES + 1):
            if attempt > 1:
                evidence = run()
            verdict = bounded(classify(evidence), evidence)
            if verdict != INCONCLUSIVE:
                return evidence, verdict, attempt
        return evidence, verdict, INCONCLUSIVE_RETRIES

    def run_all(self, models: dict[str, str]) -> dict[str, bool]:
        key = self.jobs.probe_key()
        if key is None:
            raise ApiError(409, "images_not_pinned", "sandbox image digests or probe hashes are not recorded")
        try:
            require_running(self.jobs.settings.control_dir, "sandbox_job:probe")
            require_up(self.jobs.settings.control_dir, "sandbox_job:probe")
        except Blocked as exc:
            raise ApiError(423, exc.flags.state, str(exc)) from exc
        except NotUp as exc:
            raise ApiError(423, exc.info.state, str(exc)) from exc
        for role in ("planner", "coder"):
            try:
                self.jobs.catalog.validate(models[role])
            except (KeyError, UnknownModel) as exc:
                raise ApiError(400, "unknown_model", f"{role} model invalid") from exc
        trust, scanner = key.trust_mode, self.jobs.scanner
        results: dict[str, bool] = {}
        evidence: dict[str, ProbeEvidence] = {}
        verdicts: dict[str, Verdict] = {}
        attempts: dict[str, int] = {}
        self._fixture()
        evidence["settings"] = self._run(
            self._plan("settings", "research", "probe", "none", {"OSSBOT_PROBE": "settings"})
        )
        results["settings"] = eval_settings(evidence["settings"], scanner)
        # research_deny in two sessions (models refuse a prompt that names secret files): part 1, the
        # harmless targets under every denied root, decides PASS and is retried when inconclusive;
        # part 2 names the secret paths once: a refusal there is fine, any attempted read must be denied.
        planner_deny = self._kiro_env("planner", "research_deny", models["planner"], trust)
        planner_secrets = self._kiro_env("planner", "research_deny_secrets", models["planner"], trust)
        secrets_part = self._run(self._plan("research_deny_secrets", "research", "kiro", "none", planner_secrets))

        def deny_run() -> ProbeEvidence:
            part1 = self._run(self._plan("research_deny", "research", "kiro", "none", planner_deny))
            files = {**{f"benign/{k}": v for k, v in part1.files.items()},
                     **{f"secrets/{k}": v for k, v in secrets_part.files.items()}}  # fmt: skip
            return ProbeEvidence(files=files, exit_code=part1.exit_code, timed_out=part1.timed_out)

        evidence["research_deny"], verdicts["research_deny"], attempts["research_deny"] = self._retry(
            deny_run, lambda ev: classify_research_deny(ev, scanner)
        )
        planner_sym = {**self._kiro_env("planner", "symlink", models["planner"], trust), "OSSBOT_LIST_REPO": "1"}

        def symlink_run() -> ProbeEvidence:
            sym = self._run(self._plan("symlink", "research", "kiro", "snapshot", planner_sym))
            listing = sym.files.get("repo-listing.txt", b"").decode("utf-8", "replace")
            return ProbeEvidence(files=sym.files, exit_code=sym.exit_code, snapshot_listing=listing,
                                 timed_out=sym.timed_out)  # fmt: skip

        evidence["symlink"], verdicts["symlink"], attempts["symlink"] = self._retry(
            symlink_run, lambda ev: classify_symlink(ev, scanner)
        )
        coder_env = self._kiro_env("coder", "coder_surface", models["coder"], trust)
        evidence["coder_surface"], verdicts["coder_surface"], attempts["coder_surface"] = self._retry(
            lambda: self._run(self._plan("coder_surface", "coder", "kiro", "rw", coder_env)),
            lambda ev: classify_coder_surface(ev, scanner, TRUST_TOOLS["coder"].split(",")),
        )
        mark = self.proxy_log()
        planner_env = self._kiro_env("planner", "workspace", models["planner"], trust)
        coder_env = self._kiro_env("coder", "workspace", models["coder"], trust)
        ws_planner = self._run(self._plan("workspace", "research", "kiro", "snapshot", planner_env))
        ws_coder = self._run(self._plan("workspace-coder", "coder", "kiro", "rw", coder_env))
        files = {**{f"planner/{k}": v for k, v in ws_planner.files.items()},
                 **{f"coder/{k}": v for k, v in ws_coder.files.items()}}  # fmt: skip
        proxy = self.proxy_log()[len(mark) :]
        canary = any(name.endswith("CANARY") for name in files)
        evidence["workspace"] = ProbeEvidence(files=files, proxy_log=proxy, canary_present=canary,
                                              timed_out=ws_planner.timed_out or ws_coder.timed_out)  # fmt: skip
        results["workspace"] = eval_workspace(evidence["workspace"], scanner)
        for probe in ("settings", "workspace"):  # deterministic: no model judgement involved
            verdicts[probe] = PASS if results[probe] else FAIL
        verdicts["workspace"] = bounded(verdicts["workspace"], evidence["workspace"])  # a cut session proves nothing
        results["workspace"] = verdicts["workspace"] == PASS
        for probe in MODEL_PROBES:
            results[probe] = verdicts[probe] == PASS  # inconclusive never counts as a pass
        for probe in PROBES:
            digest = evidence_digest(evidence[probe])
            self.jobs.probes.record(key, probe, results[probe], digest)
            try:
                self.jobs.gatekeeper.probe_result(key.sha256, probe, results[probe], digest,
                                                  verdict=verdicts[probe], attempts=attempts.get(probe, 1))  # fmt: skip
            except Exception:
                log.exception("probe-result report failed")
        failed = [p for p in PROBES if verdicts[p] == FAIL]
        if failed:  # a real failure: the gate stays closed and the pipeline pauses (soft stop)
            self.jobs._pause(f"safety_probe_failed:{failed[0]}")
        return results
