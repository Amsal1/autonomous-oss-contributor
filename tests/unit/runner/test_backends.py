"""Pipeline model backends (ADR-0022): the interface, config selection, and a job spec byte-equal to
the one the pre-move ``runner.llm.Kiro._once`` built."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

import pytest

from ossbot.common.backends import KIRO_HEADLESS
from ossbot.common.models import ModelsFile
from ossbot.runner import llm
from ossbot.runner.backends import (
    KiroHeadlessBackend,
    ModelBackend,
    UnknownBackend,
    base,
    select_backend,
    select_backend_named,
)
from ossbot.runner.llm import Kiro, StageFailed
from ossbot.runner.outputs import Plan
from ossbot.runner.pipeline import StageContext
from tests.unit.runner.conftest import MODELS, FakeSbx, fenced

PLAN = {"feasible": True, "summary": "guard empty spans", "validation_commands": [{"name": "unit", "cmd": "npm test"}]}
# json.dumps of the spec Kiro._once built before the move (captured from the pre-move code, run id fixed,
# input paths relative to the runner's inputs root)
SNAPSHOT = (
    '{"run_id": "planner-0a0a0a0a0a0a", "cid": "c_0000000001", "profile": "research", "entrypoint": "kiro",'
    ' "repo_mode": "snapshot", "args": {"role": "planner", "prompt_file": "planner-task.md",'
    ' "model": "claude-opus-5.5", "effort": "high"}, "inputs": {"notes.md":'
    ' "c_0000000001/planner-0a0a0a0a0a0a/notes.md", "planner-task.md":'
    ' "c_0000000001/planner-0a0a0a0a0a0a/planner-task.md"}, "timeout_s": 900}'
)


def _relative(spec: dict[str, Any], root: Path) -> str:
    inputs = {k: str(Path(v).relative_to(root)) for k, v in spec["inputs"].items()}
    return json.dumps({**spec, "inputs": inputs})


@pytest.mark.parametrize("call", ["invoke", "run_role", "stage_context"])
def test_job_spec_is_byte_equal_to_the_pre_move_snapshot(
    sc: StageContext, sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    monkeypatch.setattr(secrets, "token_hex", lambda n: "0a" * n)
    sbx.kiro("planner", fenced(PLAN))
    backend = sc.kiro() if call == "stage_context" else Kiro(sc.gk, sc.sbx, sc.models, sc.inputs_root)
    run = backend.invoke if call == "invoke" else backend.run_role
    result = run("c_0000000001", "planner", Plan, "task", {"notes.md": "n"}, timeout_s=900)
    assert _relative(sbx.jobs[-1], sc.inputs_root) == SNAPSHOT
    assert result.value.feasible and result.run_ids == ["planner-0a0a0a0a0a0a"]


@pytest.mark.parametrize("egress", ["mode", "strict"])
def test_egress_goes_into_the_spec_when_given(sc: StageContext, sbx: FakeSbx, monkeypatch: pytest.MonkeyPatch,
                                              egress: str) -> None:  # fmt: skip
    """ADR-0024: the coder's job spec carries the target's egress (and the repair retry keeps it); without the
    keyword the spec is unchanged (the snapshot test above)."""
    monkeypatch.setattr(secrets, "token_hex", lambda n: "0a" * n)
    sbx.kiro("coder", "no json here", fenced(PLAN))  # the first answer is invalid: one repair retry
    backend = sc.kiro()
    backend.invoke("c_0000000001", "coder", Plan, "task", timeout_s=900, egress=egress)  # type: ignore[arg-type]
    assert [(j["profile"], j["egress"]) for j in sbx.jobs[-2:]] == [("coder", egress), ("coder", egress)]


def test_kiro_conforms_to_the_interface(sc: StageContext) -> None:
    backend = sc.kiro()
    assert isinstance(backend, ModelBackend) and isinstance(backend, KiroHeadlessBackend)
    assert Kiro is KiroHeadlessBackend and llm.LlmResult is base.LlmResult and llm.ROLE_PROFILE is base.ROLE_PROFILE
    assert backend.probe_surface() is KIRO_HEADLESS
    assert {row["model_id"] for row in backend.list_models()} == set(MODELS)


def test_select_backend_returns_kiro_and_refuses_others(sc: StageContext) -> None:
    assert select_backend(sc.models.get()) is KiroHeadlessBackend
    assert select_backend_named("kiro") is KiroHeadlessBackend
    for name in ("kiro-acp", "claude", "codex", "openai"):
        with pytest.raises(UnknownBackend):
            select_backend_named(name)


def test_unreadable_models_keep_the_default_backend_and_fail_as_before(sc: StageContext, sbx: FakeSbx) -> None:
    sc.models.path.write_text("version: [", encoding="utf-8")  # invalid and no last good copy
    sc.models = ModelsFile(sc.models.path)
    backend = sc.kiro()
    assert type(backend) is KiroHeadlessBackend
    with pytest.raises(StageFailed) as info:
        backend.invoke("c_0000000001", "planner", Plan, "t")
    assert info.value.error_class == "model_unavailable" and sbx.jobs == []
