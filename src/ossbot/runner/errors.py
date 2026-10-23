"""Stage outcomes other than success, and the one place sandboxd answers become them.

* ``StageFailed``: the stage cannot finish (item -> FAILED, stage run ``failed``).
* ``Interrupted``: stop now, re-queue later (kill, pause, drain, down, host sleep, a sandbox job
  cut by a drain timeout or a sandboxd crash, sandboxd unreachable). Stage run ``interrupted``;
  the gatekeeper allows 3 re-queues per stage (§9.3, CR-001).
* ``Deferred``: not now and not counted (daily cap, waiting for a claim, the probe gate, an egress-anomaly
  hold); stage run ``cancelled``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from ossbot.runner.sbx_client import SbxError

INTERRUPTED = "interrupted"  # sandboxd's job error_class after a drain timeout or crash recovery
PROBE_GATE = "safety_probe_missing"  # sandboxd's 423 for a Kiro job before the probes passed (§10.9)
# open-discovery design §13.2/§13.3: the egress meter stopped the job / a hold isn't recorded yet / is open
EGRESS_ANOMALY = "egress_anomaly"
EGRESS_ANOMALY_PENDING = "egress_anomaly_pending"
CONTRIBUTION_HELD = "contribution_held"
SUMMARY_MAX = 1000  # an owner-facing summary in terminal evidence (far below EVIDENCE_MAX_BYTES, 64 KiB)


class StageFailed(RuntimeError):
    """A stage cannot finish; ``error_class`` goes to the transition evidence and the stage run. ``summary``
    (optional, ≤ ``SUMMARY_MAX``) is the owner-facing why: the FAILED evidence carries it to the gatekeeper's
    terminal FYI (DESIGN §14.13.1). ``sandbox_gap`` marks a failure after the coder reported checks it could
    not run in the sandbox (§9.8); the evidence then carries ``"sandbox_gap": true`` for the FYI's next step."""

    def __init__(self, error_class: str, message: str = "", *, summary: str = "", sandbox_gap: bool = False) -> None:
        super().__init__(f"{error_class}: {message}" if message else error_class)
        self.error_class = error_class[:64]
        self.detail = message[:200]
        self.summary = summary[:SUMMARY_MAX]
        self.sandbox_gap = sandbox_gap


class OutputInvalidFailed(StageFailed):
    """``output_invalid``: the answer failed the schema after the one repair retry. ``last_text`` is the last
    answer's assistant text (untrusted, capped by ``output_parse.MAX_TEXT``) so a stage may read a narrower
    verdict from it (the planner's ``feasible: false``, §9.7); everything else treats it as ``StageFailed``."""

    def __init__(self, message: str, last_text: str) -> None:
        super().__init__("output_invalid", message)
        self.last_text = last_text


class Interrupted(RuntimeError):
    """The stage must stop now and be re-queued later (kill, pause, drain, down, host sleep)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Deferred(RuntimeError):
    """Not now, without counting as an interrupt (daily cap, waiting for a claim); run cancelled."""


class ProbesNotReady(Deferred):
    """sandboxd refused a Kiro job: the safety probes have not passed for the current tuple (M11)."""


class CapReached(Deferred):
    """``proposal.max_per_day`` is used up; autonomous scouting waits until ``until``."""

    def __init__(self, until: datetime | None) -> None:
        super().__init__("proposal_daily_cap")
        self.until = until


class ContributionHeld(Deferred):
    """The contribution is held for an egress anomaly (open-discovery design §13.2, §13.3): the gatekeeper's
    423 ``contribution_held``, sandboxd's 423 ``egress_anomaly_pending`` or a job stopped by the meter
    (``egress_anomaly``). Not a stage failure: the run is cancelled until the owner resumes the hold."""


class JobRunner(Protocol):
    def run(self, spec: dict[str, Any], *, tick: Callable[[], None] | None = None) -> dict[str, Any]: ...


def sandbox_run(sbx: JobRunner, spec: dict[str, Any], tick: Callable[[], None] | None) -> dict[str, Any]:
    """Run one sandbox job; sandboxd stops and outages become ``Interrupted``/``ProbesNotReady``.

    The returned job may still be ``failed`` for a real failure; the caller turns that into
    ``StageFailed`` with its own error class.
    """
    try:
        done = sbx.run(spec, tick=tick)
    except SbxError as exc:
        if exc.status == 423 and exc.code == PROBE_GATE:
            raise ProbesNotReady(PROBE_GATE) from exc
        if exc.status == 423 and exc.code == EGRESS_ANOMALY_PENDING:  # the hold isn't recorded yet (fail closed)
            raise ContributionHeld(f"sandboxd {exc.status} {exc.code}") from exc
        if exc.status in {423, 503}:  # paused/killed/draining/down, or sandboxd restarting
            raise Interrupted(f"sandboxd {exc.status} {exc.code}") from exc
        raise
    if done.get("state") == "failed" and done.get("error_class") == INTERRUPTED:
        raise Interrupted(f"sandbox job {spec.get('run_id')} interrupted")
    if done.get("state") == "failed" and done.get("error_class") == EGRESS_ANOMALY:
        raise ContributionHeld(f"sandbox job {spec.get('run_id')} stopped: egress anomaly")
    return done
