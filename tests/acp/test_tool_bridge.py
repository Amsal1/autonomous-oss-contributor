"""Hermes' own tools over the text bridge (DESIGN §15.2; review FEAT-007 round 2, F007-R2-2).

ACP has no tools channel: Hermes' tool schemas go into the prompt as text and ``<tool_call>`` blocks are
parsed back out of the answer. Models do not always write the OpenAI shape, so the provider falls back
to a lenient parse (offered tools only) when Hermes' parser found nothing, and logs metadata only.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.acp.conftest import Bridge, FakeRun, client, v3

TOOLS = [
    {"type": "function", "function": {"name": "ossbot_status", "description": "s", "parameters": {}}},
    {"type": "function", "function": {"name": "ossbot_request_sandbox_job", "parameters": {}}},
    {"type": "function", "function": {"description": "nameless"}},
]
OFFERED = ["ossbot_status", "ossbot_request_sandbox_job"]


def test_tool_names_skip_malformed_entries() -> None:
    assert v3.tool_names(TOOLS) == OFFERED
    assert v3.tool_names(None) == [] and v3.tool_names("x") == []


@pytest.mark.parametrize(
    "block",
    [
        {"name": "ossbot_request_sandbox_job", "arguments": {"kind": "code", "files": ["a_1"]}},
        {"name": "ossbot_request_sandbox_job", "parameters": {"kind": "code", "files": ["a_1"]}},
        {"function": {"name": "ossbot_request_sandbox_job", "arguments": {"kind": "code", "files": ["a_1"]}}},
        {"name": "ossbot_request_sandbox_job", "arguments": json.dumps({"kind": "code", "files": ["a_1"]})},
    ],
)
def test_lenient_shapes_become_calls(block: dict[str, Any]) -> None:
    text = f"Running it.\n<tool_call>\n{json.dumps(block)}\n</tool_call>\n"
    found = v3.extract_tool_calls(Bridge(), text, OFFERED)
    assert found.blocks == 1 and len(found.calls) == 1 and found.rejected == {}
    assert found.calls[0].function.name == "ossbot_request_sandbox_job"
    assert json.loads(found.calls[0].function.arguments) == {"kind": "code", "files": ["a_1"]}
    assert found.calls[0].id == "acp_call_1"
    assert found.cleaned == "Running it."


def test_lenient_parse_handles_a_missing_close_tag_and_nested_braces() -> None:
    text = '<tool_call>{"name": "ossbot_status", "arguments": {"filter": {"a": 1}}}'
    found = v3.extract_tool_calls(Bridge(), text, OFFERED)
    assert [c.function.name for c in found.calls] == ["ossbot_status"] and found.cleaned == ""


def test_raw_newlines_inside_json_strings_are_accepted() -> None:
    # what the live model wrote for a program: real line breaks inside the JSON string values
    text = '<tool_call>{"name": "ossbot_request_sandbox_job", "arguments": {"code": "import os\nprint(1)"}}</tool_call>'
    found = v3.extract_tool_calls(Bridge(), text, OFFERED)
    assert json.loads(found.calls[0].function.arguments) == {"code": "import os\nprint(1)"}
    text = '<tool_call>{"name": "ossbot_status", "arguments": "{\\"x\\": \\"a\nb\\"}"}</tool_call>'
    assert json.loads(v3.extract_tool_calls(Bridge(), text, OFFERED).calls[0].function.arguments) == {"x": "a\nb"}


def test_invented_tool_results_after_the_calls_are_dropped() -> None:
    text = (
        'Checking.\n<tool_call>{"name": "ossbot_status"}</tool_call>\n'
        'Tool:\n{"success": true, "data": "made up"}\nSo the answer is 42.'
    )
    found = v3.extract_tool_calls(Bridge(), text, OFFERED)
    assert len(found.calls) == 1 and found.cleaned == "Checking."


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ('{"name": "terminal", "arguments": {"cmd": "id"}}', "unoffered"),  # not offered in this completion
        ('{"name": "ossbot_status", "arguments": "not json"}', "args"),
        ('{"name": "ossbot_status", "arguments": [1]}', "args"),
        ('{"tool": "ossbot_status"}', "shape"),
        ("not json at all", "json(Expecting value)"),
        ('{"name": "ossbot_status" "arguments": {}}', "json(Expecting ',' delimiter)"),
    ],
)
def test_lenient_parse_refuses_unoffered_or_malformed(body: str, reason: str) -> None:
    text = f"<tool_call>{body}</tool_call>"
    found = v3.extract_tool_calls(Bridge(), text, OFFERED)
    assert found.calls == [] and found.blocks == 1 and found.cleaned == text
    assert found.rejected == {reason: 1}


def test_hermes_parser_wins_when_it_found_calls() -> None:
    hermes_call = SimpleNamespace(id="x", function=SimpleNamespace(name="ossbot_status", arguments="{}"))

    class Parsing(Bridge):
        @staticmethod
        def extract_tool_calls_from_text(text: str) -> tuple[list[Any], str]:
            return [hermes_call], "rest"

    found = v3.extract_tool_calls(Parsing(), 'Ok.<tool_call>{"name": "ossbot_status"}</tool_call>', OFFERED)
    assert found.calls == [hermes_call] and found.cleaned == "Ok."


def test_no_tools_offered_means_no_calls() -> None:
    found = v3.extract_tool_calls(Bridge(), '<tool_call>{"name": "ossbot_status"}</tool_call>', [])
    assert found.calls == [] and found.rejected == {}

    class Parsing(Bridge):
        @staticmethod
        def extract_tool_calls_from_text(text: str) -> tuple[list[Any], str]:
            return [SimpleNamespace(id="x")], "A title"

    found = v3.extract_tool_calls(Parsing(), '<tool_call>{"name": "ossbot_status"}</tool_call>', [])
    assert found.calls == [] and found.cleaned == "A title" and found.rejected == {"unoffered": 1}


def test_hermes_string_arguments_contract_becomes_an_object() -> None:
    class Hermes(Bridge):
        @staticmethod
        def render_tool_bridge_sections(tools: Any, tool_choice: Any) -> list[str]:
            return ["Available tools. arguments must be a JSON string.\n[...]"]

    prompt = v3.format_prompt([{"role": "user", "content": "hi"}], TOOLS[:2], None, Hermes())
    assert "arguments must be a JSON string" not in prompt and "arguments is a JSON object." in prompt
    assert '"arguments": {<the arguments as a JSON object>}' in prompt


def test_tool_instructions_only_when_tools_are_offered() -> None:
    msgs = [{"role": "user", "content": "hi"}]
    plain = v3.format_prompt(msgs, None, None, Bridge())
    assert "<tool_call>" not in plain and "are real" not in plain
    with_tools = v3.format_prompt(msgs, TOOLS[:2], None, Bridge())
    assert "are real" in with_tools
    assert with_tools.splitlines()[-3] == (
        "Hermes tools you can call now with a <tool_call> block: ossbot_status, ossbot_request_sandbox_job."
    )


def test_lenient_calls_use_the_bridge_builder_when_present() -> None:
    built: list[dict[str, str]] = []

    class Building(Bridge):
        @staticmethod
        def build_openai_tool_call(*, call_id: str, name: str, arguments: str) -> Any:
            built.append({"id": call_id, "name": name, "arguments": arguments})
            return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))

    v3.extract_tool_calls(Building(), '<tool_call>{"id": "c9", "name": "ossbot_status"}</tool_call>', OFFERED)
    assert built == [{"id": "c9", "name": "ossbot_status", "arguments": "{}"}]


def test_completion_carries_calls_and_logs_metadata_only(
    fake: Callable[..., FakeRun], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret_args = {"kind": "code", "code": "print('private text')"}
    answer = f"<tool_call>{json.dumps({'name': 'ossbot_request_sandbox_job', 'arguments': secret_args})}</tool_call>"
    run = fake(prompts=[[{"text": answer}]])
    c = client(run, tmp_path)
    caplog.set_level(logging.INFO, logger="kiro_acp.v3")
    out = c.chat.completions.create(
        model="kiro-acp", messages=[{"role": "user", "content": "run it"}], tools=TOOLS[:2], timeout=20
    )
    choice = out.choices[0]
    assert choice.finish_reason == "tool_calls" and choice.message.content == ""
    assert json.loads(choice.message.tool_calls[0].function.arguments) == secret_args
    prompt = run.requests("session/prompt")[0]["params"]["prompt"][0]["text"]
    assert "Tools: ossbot_status, ossbot_request_sandbox_job" in prompt and "are real" in prompt
    lines = [r.getMessage() for r in caplog.records if "kiro_acp tools" in r.getMessage()]
    assert lines == [
        "kiro_acp tools: offered=2 bridged=True blocks=1 parsed=1 rejected=- "
        "names=ossbot_request_sandbox_job,ossbot_status"
    ]
    assert all("private text" not in r.getMessage() for r in caplog.records)
    c.close()


def test_completion_is_awaitable_for_hermes_async_auxiliary_path(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    run = fake(prompts=[[{"text": "a description"}]])
    c = client(run, tmp_path)

    async def aux() -> Any:
        return await c.chat.completions.create(
            model="kiro-acp", messages=[{"role": "user", "content": "describe"}], timeout=20
        )

    out = asyncio.run(aux())
    assert out.choices[0].message.content == "a description"
    c.close()
