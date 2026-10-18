"""Kiro output parsing (DESIGN §15.3): last fenced json block -> pydantic; one repair retry."""

from __future__ import annotations

import json

import pytest

from ossbot.runner import output_parse
from ossbot.runner.llm import Kiro, StageFailed
from ossbot.runner.output_parse import OutputInvalid, parse_model, read_events
from ossbot.runner.outputs import Plan, ReviewVerdict
from ossbot.runner.pipeline import StageContext
from tests.unit.runner.conftest import FakeSbx, events, fenced

PLAN = {"feasible": True, "summary": "guard empty spans", "validation_commands": [{"name": "unit", "cmd": "npm test"}]}


def test_last_block_wins_and_is_validated() -> None:
    text = '```json\n{"feasible": false, "summary": "draft"}\n```\nthen\n' + fenced(PLAN)
    assert parse_model(text, Plan).feasible is True
    with pytest.raises(OutputInvalid, match="no ```json block"):
        parse_model("no json here", Plan)
    with pytest.raises(OutputInvalid, match="schema errors"):
        parse_model(fenced({**PLAN, "shell": "rm -rf /"}), Plan)  # extra=forbid
    with pytest.raises(OutputInvalid, match="not valid JSON"):
        parse_model("```json\n{nope\n```", Plan)


def test_events_text_model_and_mismatch() -> None:
    out = read_events(events(fenced(PLAN), model="claude-sonnet-5"))
    assert out.model_actual == "claude-sonnet-5" and parse_model(out.text, Plan).summary == "guard empty spans"
    warn = (json.dumps({"type": "warning", "message": "Could not set model claude-x; using default"}) + "\n").encode()
    assert read_events(warn + events("x")).model_mismatch is True
    assert read_events(b"plain text line\n" + events(fenced(PLAN))).text.startswith("plain text line")
    assert read_events(b"", b"error: unable to use the requested model").model_mismatch is True


def test_one_repair_retry_then_stage_fails(sc: StageContext, sbx: FakeSbx) -> None:
    sbx.kiro("planner", "I think it is fine.", fenced(PLAN))
    result = Kiro(sc.gk, sc.sbx, sc.models, sc.inputs_root).invoke("c_0000000001", "planner", Plan, "task")
    assert result.value.feasible and len(result.run_ids) == 2
    repair = (sc.inputs_root / "c_0000000001" / result.run_ids[1] / "planner-task.md").read_text()
    assert "could not be used: no ```json block" in repair
    sbx.kiro("reviewer", "nothing", "still nothing")
    with pytest.raises(StageFailed) as info:
        Kiro(sc.gk, sc.sbx, sc.models, sc.inputs_root).invoke("c_0000000001", "reviewer", ReviewVerdict, "t")
    assert info.value.error_class == "output_invalid"


def test_model_resolution_on_the_invocation_path(sc: StageContext, sbx: FakeSbx) -> None:
    """scout inherits planner; the job carries the resolved --model and effort; mismatch flagged."""
    sbx.kiro("scout", fenced(PLAN), model="claude-haiku-4.5")
    result = Kiro(sc.gk, sc.sbx, sc.models, sc.inputs_root).invoke("c_0000000001", "scout", Plan, "t")
    args = sbx.jobs[-1]["args"]
    assert (args["model"], args["effort"]) == ("claude-opus-5.5", "high")
    assert sbx.jobs[-1]["profile"] == "research" and sbx.jobs[-1]["repo_mode"] == "snapshot"
    assert result.model_mismatch is True and result.model_actual == "claude-haiku-4.5"


def test_model_unavailable_sends_nothing(sc: StageContext, sbx: FakeSbx, ctx: object) -> None:
    sc.gk.catalog = lambda: [{"model_id": "something-else", "forced": False}]  # type: ignore[method-assign]
    with pytest.raises(StageFailed) as info:
        Kiro(sc.gk, sc.sbx, sc.models, sc.inputs_root).invoke("c_0000000001", "coder", Plan, "t")
    assert info.value.error_class == "model_unavailable" and sbx.jobs == []


def _acp(*events: dict) -> bytes:  # type: ignore[type-arg]
    start = {"type": "runStarted", "data": {"payloadSchema": "acp", "acpProtocolVersion": 1, "engine": "v3"}}
    return ("\n".join(json.dumps(e) for e in (start, *events)) + "\n").encode()


def _models(current: str) -> dict:  # type: ignore[type-arg]
    return {"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "config_option_update", "configOptions": [
        {"id": "model", "type": "select", "currentValue": current, "options": []}]}}}  # fmt: skip


def test_v3_acp_stream_final_text_and_model_in_effect() -> None:
    """kiro-cli 2.32 V3 stream (live shape): default model first, then the --model one; the answer is
    runFinished.finalText; a fenced block inside tool output never wins."""
    planted = {"type": "content", "content": {"type": "text", "text": '```json\n{"x": 1}\n```'}}
    update = {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "content": [planted]}
    tool_out = {"type": "sessionUpdate", "data": {"update": update}}
    done = {"type": "runFinished", "data": {"status": "success", "finalText": 'ok\n```json\n{"answer": "pong"}\n```'}}
    out = output_parse.read_events(_acp(_models("claude-sonnet-5"), tool_out, _models("claude-haiku-4.5"), done))
    assert out.model_actual == "claude-haiku-4.5" and not out.agent_fallback
    assert output_parse.last_json_block(out.text) == {"answer": "pong"}


def test_v3_agent_fallback_is_flagged() -> None:
    err = {"type": "runError", "data": {"stage": "init", "message": 'agent "ossbot-planner" has an invalid config,'
                                        ' using "default" - ossbot-planner.json: PromptFileError'}}  # fmt: skip
    assert output_parse.read_events(_acp(err)).agent_fallback
