"""Runner test fixtures: the real gatekeeper app in-process (worker peer, sync TestClient), a fake
sandboxd that writes canned job outputs, and the fake GitHub on the runner's read client."""

from __future__ import annotations

import json
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():  # starlette's TestClient warns about plain httpx; it is only a test transport
    warnings.simplefilter("ignore", DeprecationWarning)
    from starlette.testclient import TestClient

from ossbot.common.models import ModelsFile
from ossbot.common.schemas import CatalogEntry
from ossbot.gatekeeper.app import create_app
from ossbot.gatekeeper.context import AppContext
from ossbot.runner.github_read import GitHubRead
from ossbot.runner.gk_client import Gatekeeper
from ossbot.runner.pipeline import Clocks, Gate, StageContext
from ossbot.runner.sbx_client import SbxError
from tests.conftest import UID_WORKER
from tests.fakes.fake_github import FakeGitHub
from tests.unit.gatekeeper.api_helpers import InjectPeer

Handler = Callable[[dict[str, Any], Path], int]  # (spec, output dir) -> exit code (0 = succeeded)
MODELS = ("claude-opus-5.5", "claude-sonnet-5", "claude-sonnet-5.5", "claude-haiku-4.5", "claude-opus-5")


def fenced(doc: dict[str, Any]) -> str:
    return "Done.\n```json\n" + json.dumps(doc) + "\n```\n"


def events(text: str, model: str = "claude-opus-5.5") -> bytes:
    lines = [{"type": "session", "model": model}, {"type": "assistant_message", "content": text}]
    return ("\n".join(json.dumps(x) for x in lines) + "\n").encode()


def acp_events(text: str, tool_calls: list[dict[str, Any]] | None = None) -> bytes:
    """A V3 ACP stream (kiro-cli 2.32 shapes, ``sandboxd/probes.py``): ``runStarted``, one
    ``tool_call`` + ``tool_call_update`` per call (``kind``, ``rawInput``, ``status`` default
    ``completed``, optional ``locations`` and ``output``), then ``runFinished.finalText``."""
    lines: list[dict[str, Any]] = [{"type": "runStarted", "data": {"payloadSchema": "acp"}}]
    for i, call in enumerate(tool_calls or []):
        tid = f"tooluse_{i}"
        first: dict[str, Any] = {"sessionUpdate": "tool_call", "toolCallId": tid, "title": call.get("title", "Read"),
                                 "kind": call.get("kind", "read"), "rawInput": call.get("rawInput", {}),
                                 "status": "pending"}  # fmt: skip
        if "locations" in call:
            first["locations"] = call["locations"]
        output = {"type": "content", "content": {"type": "text", "text": call.get("output", "ok")}}
        done = {"sessionUpdate": "tool_call_update", "toolCallId": tid, "status": call.get("status", "completed"),
                "content": [output]}  # fmt: skip
        lines += [{"type": "sessionUpdate", "data": {"update": first}},
                  {"type": "sessionUpdate", "data": {"update": done}}]  # fmt: skip
    lines.append({"type": "runFinished", "data": {"finalText": text}})
    return ("\n".join(json.dumps(x) for x in lines) + "\n").encode()


def fetch_json(spec: dict[str, Any], out: Path, *, has_test_suite: bool = True) -> int:
    """What fetch.sh writes (the scout clone and the build clone)."""
    doc = {"base_sha": "1" * 40, "base_ref": "main", "ecosystems": ["node"], "test_files": 12 if has_test_suite else 0,
           "has_test_suite": has_test_suite}  # fmt: skip
    (out / "fetch.json").write_text(json.dumps(doc))
    return 0


@dataclass
class FakeSbx:
    """Stand-in for sandboxd: ``handlers[entrypoint]`` or ``handlers["kiro:<role>"]`` write outputs."""

    root: Path
    handlers: dict[str, Handler] = field(default_factory=dict)
    jobs: list[dict[str, Any]] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    refuse: dict[str, tuple[int, str]] = field(default_factory=dict)  # entrypoint -> (status, code)
    interrupt: set[str] = field(default_factory=set)  # entrypoints sandboxd cuts (drain timeout / crash)
    probes_passed: bool = True
    probes_pass_on_run: bool = True
    probe_runs: list[dict[str, str]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)  # M11 order: list_models, probes_status, probes_run, kiro:<role>
    error_class: dict[str, str] = field(default_factory=dict)  # handler key -> error_class of a failed job
    destroyed: list[str] = field(default_factory=list)  # POST /v1/tasks/{cid}/destroy

    def run(self, spec: dict[str, Any], *, tick: Callable[[], None] | None = None, poll_s: float = 0) -> dict[str, Any]:
        key = f"kiro:{spec['args']['role']}" if spec["entrypoint"] == "kiro" else spec["entrypoint"]
        self.calls.append(key)
        if spec["entrypoint"] == "kiro" and not self.probes_passed:
            raise SbxError(423, "safety_probe_missing")
        if spec["entrypoint"] in self.refuse:
            status, code = self.refuse[spec["entrypoint"]]
            raise SbxError(status, code)
        if (spec["profile"] in {"fetch", "validate"}) != ("egress" in spec):  # sandboxd validate_job (§12.3)
            raise SbxError(400, "egress_required" if "egress" not in spec else "egress_not_applicable")
        self.jobs.append(spec)
        if spec["entrypoint"] in self.interrupt:
            return {"run_id": spec["run_id"], "state": "failed", "artifact_dir": None, "error_class": "interrupted"}
        if tick is not None:
            try:
                tick()
            except BaseException:
                self.cancelled.append(spec["run_id"])
                raise
        out = self.root / spec["cid"] / "runs" / spec["run_id"]
        out.mkdir(parents=True, exist_ok=True)
        handler = self.handlers.get(key) or (fetch_json if key == "fetch" else None)
        code = handler(spec, out) if handler is not None else 0
        state = "succeeded" if code == 0 else "failed"
        error = self.error_class.get(key, f"{spec['entrypoint']}_exit_{code}")  # result.json error_class
        return {"run_id": spec["run_id"], "state": state, "artifact_dir": str(out) if code == 0 else None,
                "error_class": None if code == 0 else error}  # fmt: skip

    def destroy(self, cid: str) -> None:
        self.destroyed.append(cid)

    def kiro(self, role: str, *answers: str, model: str = "claude-opus-5.5") -> None:
        """Canned answers for a role, in order (the last one repeats)."""
        queue = list(answers)

        def handler(spec: dict[str, Any], out: Path) -> int:
            text = queue.pop(0) if len(queue) > 1 else queue[0]
            (out / f"{role}.events.jsonl").write_bytes(events(text, model))
            return 0

        self.handlers[f"kiro:{role}"] = handler

    def probes_status(self) -> dict[str, Any]:
        self.calls.append("probes_status")
        return {"all_passed": self.probes_passed}

    def probes_run(self, models: dict[str, str]) -> dict[str, Any]:
        self.calls.append("probes_run")
        self.probe_runs.append(models)
        self.probes_passed = self.probes_pass_on_run
        return {"results": {}}

    def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)


@dataclass
class FakeClocks(Clocks):
    m: float = 1000.0
    w: float = 1_790_000_000.0

    def __post_init__(self) -> None:
        self.monotonic = lambda: self.m
        self.wall = lambda: self.w

    def advance(self, seconds: float, *, slept: float = 0.0) -> None:
        self.m += seconds
        self.w += seconds + slept


@pytest.fixture
def github() -> FakeGitHub:
    gh = FakeGitHub()
    gh.add_repo("langfuse/langfuse", default_branch="main", language="TypeScript")
    return gh


@pytest.fixture
def gk(ctx: AppContext) -> Iterator[Gatekeeper]:
    app = create_app(ctx, housekeeping=False)
    with TestClient(InjectPeer(app, UID_WORKER), base_url="http://gatekeeper") as client:  # type: ignore[arg-type]
        yield Gatekeeper(client=client)


@pytest.fixture
def sbx(tmp_path: Path) -> FakeSbx:
    return FakeSbx(tmp_path / "artifacts")


@pytest.fixture
def sc(ctx: AppContext, gk: Gatekeeper, sbx: FakeSbx, github: FakeGitHub, tmp_path: Path) -> StageContext:
    ctx.models.upsert_catalog([CatalogEntry(model_id=m) for m in MODELS], "uid:2001/worker")
    return StageContext(
        gk=gk, sbx=sbx, gh=GitHubRead(github.sync_client(), sleep=lambda s: None),  # type: ignore[arg-type]
        models=ModelsFile(ctx.paths.config_dir / "models.yaml"), gate=Gate(ctx.paths.control_dir),
        inputs_root=tmp_path / "runner" / "inputs", config=lambda: ctx.config, clocks=FakeClocks(),
        state_dir=tmp_path / "runner" / "state", notify=lambda text: None,
    )  # fmt: skip
