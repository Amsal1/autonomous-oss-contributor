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


@dataclass
class FakeSbx:
    """Stand-in for sandboxd: ``handlers[entrypoint]`` or ``handlers["kiro:<role>"]`` write outputs."""

    root: Path
    handlers: dict[str, Handler] = field(default_factory=dict)
    jobs: list[dict[str, Any]] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    refuse: dict[str, tuple[int, str]] = field(default_factory=dict)  # entrypoint -> (status, code)

    def run(self, spec: dict[str, Any], *, tick: Callable[[], None] | None = None, poll_s: float = 0) -> dict[str, Any]:
        if spec["entrypoint"] in self.refuse:
            status, code = self.refuse[spec["entrypoint"]]
            raise SbxError(status, code)
        self.jobs.append(spec)
        if tick is not None:
            try:
                tick()
            except BaseException:
                self.cancelled.append(spec["run_id"])
                raise
        out = self.root / spec["cid"] / "runs" / spec["run_id"]
        out.mkdir(parents=True, exist_ok=True)
        key = f"kiro:{spec['args']['role']}" if spec["entrypoint"] == "kiro" else spec["entrypoint"]
        handler = self.handlers.get(key)
        code = handler(spec, out) if handler is not None else 0
        state = "succeeded" if code == 0 else "failed"
        return {"run_id": spec["run_id"], "state": state, "artifact_dir": str(out) if code == 0 else None,
                "error_class": None if code == 0 else f"{spec['entrypoint']}_exit_{code}"}  # fmt: skip

    def kiro(self, role: str, *answers: str, model: str = "claude-opus-5.5") -> None:
        """Canned answers for a role, in order (the last one repeats)."""
        queue = list(answers)

        def handler(spec: dict[str, Any], out: Path) -> int:
            text = queue.pop(0) if len(queue) > 1 else queue[0]
            (out / f"{role}.events.jsonl").write_bytes(events(text, model))
            return 0

        self.handlers[f"kiro:{role}"] = handler

    def probes_status(self) -> dict[str, Any]:
        return {"all_passed": True}

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
        gk=gk, sbx=sbx, gh=GitHubRead(github.sync_client()),  # type: ignore[arg-type]
        models=ModelsFile(ctx.paths.config_dir / "models.yaml"), gate=Gate(ctx.paths.control_dir),
        inputs_root=tmp_path / "runner" / "inputs", config=lambda: ctx.config, clocks=FakeClocks(),
        state_dir=tmp_path / "runner" / "state", notify=lambda text: None,
    )  # fmt: skip
