"""V3 ACP contract tests (DESIGN §22.3): the vendored adapter against ``tests/fakes/fake_acp_v3.py``.

The adapter is imported from the vendored source tree (not a possibly stale installed copy).
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
VENDOR = REPO / "vendor" / "hermes-kiro-provider"
FAKE = REPO / "tests" / "fakes" / "fake_acp_v3.py"
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

from kiro_acp import v3, v3_core  # noqa: E402


@dataclass
class FakeRun:
    scenario_path: Path
    log_path: Path
    procs: list[subprocess.Popen[bytes]]

    def spawn(self) -> subprocess.Popen[bytes]:
        proc = subprocess.Popen(
            [sys.executable, str(FAKE), str(self.scenario_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.procs.append(proc)
        return proc

    def frames(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def methods(self) -> list[str]:
        return [f["method"] for f in self.frames() if "method" in f]

    def requests(self, method: str) -> list[dict[str, Any]]:
        return [f for f in self.frames() if f.get("method") == method]

    def responses(self) -> list[dict[str, Any]]:
        return [f for f in self.frames() if "method" not in f]


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[Callable[..., FakeRun]]:
    runs: list[FakeRun] = []

    def make(**scenario: Any) -> FakeRun:
        n = len(runs)
        log = tmp_path / f"fake{n}.log"
        path = tmp_path / f"scenario{n}.json"
        path.write_text(json.dumps({"log": str(log), **scenario}), encoding="utf-8")
        run = FakeRun(path, log, [])
        runs.append(run)
        return run

    yield make
    for run in runs:
        for proc in run.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    stream.close()


class Bridge:
    """Stand-in for Hermes' ``agent.acp_openai_bridge`` (text rendering only)."""

    @staticmethod
    def render_tool_bridge_sections(tools: Any, tool_choice: Any) -> list[str]:
        return ["Tools: " + ", ".join(t["function"]["name"] for t in tools or [])] if tools else []

    @staticmethod
    def extract_tool_calls_from_text(text: str) -> tuple[list[Any], str]:
        return [], text

    @staticmethod
    def completion_to_stream_chunks(completion: Any) -> list[Any]:
        return [completion]


def models_file(
    tmp_path: Path, chat: str = "claude-sonnet-5.5", effort: str = "medium", vision: str = "claude-opus-5.5"
) -> Path:
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps(
            {
                "default": {"model": "claude-opus-5.5", "effort": "high"},
                "roles": {"chat": {"model": chat, "effort": effort}, "vision": {"model": vision, "effort": "medium"}},
                "fallbacks": ["claude-opus-5.5", "claude-sonnet-5.5"],
            }
        ),
        encoding="utf-8",
    )
    return path


def client(
    run: FakeRun, tmp_path: Path, *, models: Path | None = None, hook: Any = None, **settings: Any
) -> v3.KiroV3Client:
    path = models or models_file(tmp_path)
    s = v3.V3Settings(models_file=str(path), **settings)
    return v3.KiroV3Client(
        settings=s, spawn=run.spawn, bridge=Bridge(), on_surface_violation=hook or (lambda exc: None)
    )


def ask(c: v3.KiroV3Client, text: str = "hello", **kw: Any) -> Any:
    return c.chat.completions.create(model="kiro-acp", messages=[{"role": "user", "content": text}], timeout=20, **kw)


__all__ = ["VENDOR", "Bridge", "FakeRun", "ask", "client", "models_file", "v3", "v3_core"]
