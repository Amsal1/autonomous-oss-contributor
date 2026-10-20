"""Stage execution core (DESIGN §9.1, §9.3, §13.4, §13.5 CR-001).

* ``Gate.check`` runs ``require_running`` and ``require_up`` before every stage: kill, pause,
  draining and down start nothing new. At every heartbeat only kill and pause are checked
  (``Gate.check_running``): a drain lets running work finish (§13.5); sandboxd refuses the next
  job of a multi-job stage while draining, and that refusal, like a job sandboxd cut at the drain
  timeout, interrupts the stage, which is re-queued on ``up`` (max 3).
* ``StageRun`` opens the gatekeeper ``stage_runs`` row (refused there too while stopped), heartbeats
  every 15 s, enforces the stage timeout on the monotonic clock and treats a wall-clock gap of more
  than 300 s beyond the monotonic one as a host sleep (interrupted, not failed).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

from ossbot.common.config import ConfigBundle
from ossbot.common.control_flags import Blocked, NotUp, require_running, require_up
from ossbot.common.models import ModelsFile, ModelUnavailable
from ossbot.common.schemas import STAGE_ENTRYPOINTS
from ossbot.runner.backends import select_backend
from ossbot.runner.errors import Deferred, Interrupted, StageFailed
from ossbot.runner.github_read import GitHubRead
from ossbot.runner.gk_client import Gatekeeper, GkError
from ossbot.runner.llm import Kiro
from ossbot.runner.sbx_client import Sandbox

__all__ = ["HEARTBEAT_S", "SLEEP_GAP_S", "STAGE_TIMEOUT_S", "Clocks", "Deferred", "Gate", "Interrupted",
           "StageContext", "StageFailed", "StageRun"]  # fmt: skip
log = logging.getLogger(__name__)
SANDBOX_NOTE = "_volumes"  # runner note: cids whose sandbox resources (volume, networks) may exist
_NOTES_LOCK = threading.Lock()
HEARTBEAT_S = 15.0
SLEEP_GAP_S = 300.0
STAGE_TIMEOUT_S: dict[str, int] = {
    "discover": 600, "vet": 900, "scout": 1200, "fetch": 1800, "plan": 1200, "implement": 3600,
    "validate": 2700, "review": 1200, "package": 600, "watch": 900, "catalog": 300, "import": 1800,
}  # fmt: skip
assert set(STAGE_TIMEOUT_S) == set(STAGE_ENTRYPOINTS), "every runner stage has a timeout"


@dataclass
class Clocks:
    monotonic: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time


class Gate:
    def __init__(self, control_dir: Path) -> None:
        self.control_dir = control_dir

    def check(self, stage: str) -> None:
        what = f"stage:{stage}"
        try:
            require_running(self.control_dir, what)
            require_up(self.control_dir, what)
        except (Blocked, NotUp) as exc:
            raise Interrupted(str(exc)) from exc

    def check_running(self, stage: str) -> None:
        """Heartbeat check: kill and pause stop running work; draining/down do not (§13.5)."""
        try:
            require_running(self.control_dir, f"stage:{stage}")
        except Blocked as exc:
            raise Interrupted(str(exc)) from exc

    def open(self, stage: str) -> bool:
        try:
            self.check(stage)
        except Interrupted:
            return False
        return True


@dataclass
class StageContext:
    gk: Gatekeeper
    sbx: Sandbox
    gh: GitHubRead
    models: ModelsFile
    gate: Gate
    inputs_root: Path
    config: Callable[[], ConfigBundle]
    clocks: Clocks = field(default_factory=Clocks)
    run: StageRun | None = None  # set while a stage runs (its tick drives heartbeats)
    state_dir: Path | None = None  # runner-local notes per contribution (artifact dirs of earlier stages)
    notify: Callable[[str], None] | None = None
    # the service is stopping (SIGTERM): discovery, which holds no state, ends at the next target
    stopping: Callable[[], bool] = field(default=lambda: False)

    def load(self, cid: str) -> dict[str, Any]:
        path = (self.state_dir or self.inputs_root.parent / "state") / f"{cid}.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return doc if isinstance(doc, dict) else {}

    def save(self, cid: str, **values: Any) -> dict[str, Any]:
        directory = self.state_dir or self.inputs_root.parent / "state"
        directory.mkdir(parents=True, exist_ok=True)
        doc = {**self.load(cid), **values}
        tmp = directory / f".{cid}.json.tmp"
        tmp.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
        tmp.replace(directory / f"{cid}.json")
        return doc

    def note_sandbox(self, cid: str, present: bool = True) -> None:
        """Remember cids that ran a sandbox job: their task volume and per-task networks are destroyed
        by the scheduler's sweep once the item is terminal or has not passed Start work."""
        with _NOTES_LOCK:
            cids = set(self.load(SANDBOX_NOTE).get("cids", []))
            if (cid in cids) != present:
                self.save(SANDBOX_NOTE, cids=sorted(cids | {cid} if present else cids - {cid}))

    def sandbox_cids(self) -> list[str]:
        with _NOTES_LOCK:
            return [str(c) for c in self.load(SANDBOX_NOTE).get("cids", [])]

    def say(self, text: str) -> None:
        if self.notify is not None:
            self.notify(text)
            return
        try:
            self.gk.notify(text)
        except GkError as exc:
            log.warning("notify failed: %s", exc.code, extra={"event": "notify_failed"})

    def kiro(self) -> Kiro:
        """The pipeline backend ``config/models.yaml`` selects (ADR-0022), read live like the models.
        An unreadable file with no last good copy keeps the default, whose ``resolve`` then fails the
        stage with ``model_unavailable`` as before."""
        try:
            backend = select_backend(self.models.get())
        except ModelUnavailable:
            backend = Kiro
        return backend(self.gk, self.sbx, self.models, self.inputs_root, tick=self.tick, on_job=self.note_sandbox)

    def tick(self) -> None:
        if self.run is not None:
            self.run.tick()

    def transition(self, cid: str, from_: str, to: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.gk.transition(cid, from_, to, evidence)


class StageRun:
    """``with StageRun(sc, cid, "plan"):`` — open, heartbeat, close with the outcome."""

    def __init__(self, sc: StageContext, cid: str, stage: str, **fields: Any) -> None:
        self.sc = sc
        self.cid = cid
        self.stage = stage
        self.fields = fields
        self.timeout_s = STAGE_TIMEOUT_S.get(stage, 1800)
        self.run_id: str | None = None
        self.model_actual: str | None = None

    def __enter__(self) -> StageRun:
        self.sc.gate.check(self.stage)
        try:
            doc = self.sc.gk.stage_open(self.cid, self.stage, **self.fields)
        except GkError as exc:
            if exc.status == 423:
                raise Interrupted(f"gatekeeper refused stage {self.stage}: {exc.code}") from exc
            raise StageFailed(f"stage_open_{exc.code}"[:64]) from exc
        self.run_id = str(doc["id"])
        now_m, now_w = self.sc.clocks.monotonic(), self.sc.clocks.wall()
        self.started_m = self.last_m = self.last_beat = now_m
        self.last_w = now_w
        self.sc.run = self
        return self

    def tick(self) -> None:
        now_m, now_w = self.sc.clocks.monotonic(), self.sc.clocks.wall()
        slept = (now_w - self.last_w) - (now_m - self.last_m)
        self.last_m, self.last_w = now_m, now_w
        if slept > SLEEP_GAP_S:
            raise Interrupted(f"host slept {int(slept)} s")
        self.sc.gate.check_running(self.stage)
        if now_m - self.started_m > self.timeout_s:
            raise StageFailed("timeout", f"{self.stage} > {self.timeout_s} s")
        if now_m - self.last_beat >= HEARTBEAT_S and self.run_id is not None:
            self.last_beat = now_m
            try:
                self.sc.gk.stage_update(self.run_id)
            except GkError as exc:
                log.warning("heartbeat failed: %s", exc.code, extra={"event": "heartbeat_failed", "cid": self.cid})

    def __exit__(self, kind: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        self.sc.run = None
        if self.run_id is None:
            return
        if exc is None:
            status, error_class = "succeeded", None
        elif isinstance(exc, Interrupted):
            status, error_class = "interrupted", "interrupted"
        elif isinstance(exc, Deferred):
            status, error_class = "cancelled", "deferred"
        elif isinstance(exc, StageFailed):
            status, error_class = "failed", exc.error_class
        else:
            status, error_class = "failed", f"internal:{type(exc).__name__}"
        if error_class is not None:  # the gatekeeper accepts ^[a-z0-9_:.-]{1,64}$ only
            error_class = re.sub(r"[^a-z0-9_:.-]", "_", error_class.lower())[:64] or "error"
        try:
            self.sc.gk.stage_update(self.run_id, status=status, error_class=error_class,
                                    model_actual=self.model_actual,
                                    error_message=str(exc)[:500] if exc is not None else None)  # fmt: skip
        except GkError as err:
            log.warning("stage close failed: %s", err.code, extra={"event": "stage_close_failed", "cid": self.cid})
