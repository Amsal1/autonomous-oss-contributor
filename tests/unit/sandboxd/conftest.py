"""sandboxd fixtures: JobService over FakeRuntime with a fake gatekeeper and pinned fake images."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ossbot.common.clock import FakeClock
from ossbot.common.images import ImagePins
from ossbot.common.public_floor import FloorStatus
from ossbot.sandboxd.db import Store
from ossbot.sandboxd.jobs import HoldError, JobService, Settings
from ossbot.sandboxd.keyscan import KeyScanner
from ossbot.sandboxd.models import CatalogCache
from ossbot.sandboxd.networks import Networks
from ossbot.sandboxd.probes import PROBES, TrustMode, compute_key
from ossbot.sandboxd.runtime.fake import FakeRuntime
from ossbot.sandboxd.subnets import SubnetAllocator
from tests.sandboxharness import DIGESTS, FULL_DIGEST, LLM_DIGEST

KEY = "ksk_test_key_value_0123456789abcdef"
PINS = ImagePins(
    digests={"sandbox-full": FULL_DIGEST, "sandbox-llm": LLM_DIGEST},
    kiro_cli_version="2.32.0",
    agent_configs_sha256="c" * 64,
    settings_sha256="d" * 64,
)
KNOWN = FloorStatus(True, None, 1, 1, True)  # the public-address floor is known (ADR-0025)
UNKNOWN = FloorStatus(False, 100.0, 0, 1, True)


@dataclass
class FakeGatekeeper:
    models: list[dict[str, Any]] = field(
        default_factory=lambda: [{"model_id": "claude-test", "forced": 0}, {"model_id": "forced-x", "forced": 1}]
    )
    states: dict[str, str] = field(default_factory=dict)
    paused: list[str] = field(default_factory=list)
    probe_results: list[tuple[str, str, bool, str]] = field(default_factory=list)
    probe_verdicts: list[tuple[str, str | None, int]] = field(default_factory=list)
    catalog_calls: int = 0
    holds: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # (cid, body) of every hold call
    hold_answers: list[HoldError | None] = field(default_factory=list)  # queued answers; empty = recorded
    # ADR-0024: the per-run implement-egress decision ("web", "none", junk, or an exception to raise)
    egress_answer: Any = "none"
    egress_calls: list[tuple[str, str]] = field(default_factory=list)
    # ADR-0025: notices and egress notes (queued note answers like hold_answers)
    notices: list[tuple[str, str]] = field(default_factory=list)
    notes: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    note_answers: list[BaseException | None] = field(default_factory=list)

    def implement_egress(self, cid: str, run_id: str) -> str:
        self.egress_calls.append((cid, run_id))
        if isinstance(self.egress_answer, BaseException):
            raise self.egress_answer
        return self.egress_answer  # type: ignore[no-any-return]

    def catalog(self) -> list[dict[str, Any]]:
        self.catalog_calls += 1
        return list(self.models)

    def hold(self, cid: str, body: dict[str, Any]) -> None:
        self.holds.append((cid, body))
        answer = self.hold_answers.pop(0) if self.hold_answers else None
        if answer is not None:
            raise answer

    def pause(self, reason: str) -> None:
        self.paused.append(reason)

    def notify(self, text: str, priority: str = "P1") -> None:
        self.notices.append((text, priority))

    def egress_note(self, cid: str, body: dict[str, Any]) -> None:
        self.notes.append((cid, body))
        answer = self.note_answers.pop(0) if self.note_answers else None
        if answer is not None:
            raise answer

    def contribution_state(self, cid: str) -> str | None:
        return self.states.get(cid)

    def probe_result(self, probe_key: str, probe: str, passed: bool, evidence_sha256: str, *,
                     verdict: str | None = None, attempts: int = 1) -> None:  # fmt: skip
        self.probe_results.append((probe_key, probe, passed, evidence_sha256))
        self.probe_verdicts.append((probe, verdict, attempts))


@dataclass
class Sbx:
    jobs: JobService
    runtime: FakeRuntime
    gatekeeper: FakeGatekeeper
    clock: FakeClock
    settings: Settings
    root: Path
    trust: list[TrustMode]
    mode: list[str] = field(default_factory=lambda: ["allowlist"])  # sandboxd's live egress-mode read
    floor: list[FloorStatus] = field(default_factory=lambda: [KNOWN])  # the public-address floor read

    def runner_input(self, name: str, data: bytes) -> str:
        path = self.root / "srv" / "runner" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)


@pytest.fixture
def sbx(tmp_path: Path) -> Iterator[Sbx]:
    clock = FakeClock()
    store = Store(tmp_path / "sandboxd.db")
    control = tmp_path / "control"
    control.mkdir()
    runtime = FakeRuntime(
        tmp_path / "rt", image_digests=dict(DIGESTS), control_dir=control, environ={"KIRO_API_KEY": KEY}
    )
    gatekeeper = FakeGatekeeper()
    settings = Settings(
        artifacts_dir=tmp_path / "srv" / "artifacts",
        logs_dir=tmp_path / "srv" / "logs",
        staging_dir=tmp_path / "srv" / "sandboxd" / "staging",
        control_dir=control,
        input_roots=(tmp_path / "srv" / "runner", tmp_path / "srv" / "artifacts"),
    )
    trust: list[TrustMode] = ["ids"]
    mode = ["allowlist"]
    floor = [KNOWN]
    jobs = JobService(
        store=store,
        runtime=runtime,
        pins=lambda: PINS,
        catalog=CatalogCache(gatekeeper.catalog, clock),
        gatekeeper=gatekeeper,
        scanner=KeyScanner([KEY]),
        clock=clock,
        settings=settings,
        trust_mode=lambda: trust[0],
        networks=Networks(runtime, SubnetAllocator(store, clock)),
        background=False,
        exec_egress_mode=lambda: mode[0],
        public_floor=lambda: floor[0],
    )
    jobs.note_sleep = lambda s: None
    yield Sbx(jobs, runtime, gatekeeper, clock, settings, tmp_path, trust, mode, floor)
    store.close()


def pass_all_probes(sbx: Sbx, trust_mode: TrustMode = "ids") -> None:
    key = compute_key(PINS, trust_mode)
    assert key is not None
    for probe in PROBES:
        sbx.jobs.probes.record(key, probe, True, "e" * 64)
