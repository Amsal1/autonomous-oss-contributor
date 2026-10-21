"""The PR body and title apply the repo's recorded AI-policy conditions (DESIGN §9.5, §9.11, post-v1)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ossbot.common.ai_policy import CHECKLIST_HEADER, AiCondition, missing_conditions
from ossbot.common.project import PROJECT_URL
from ossbot.runner.outputs import PrDraft
from ossbot.runner.stages import package

pytestmark = pytest.mark.unit

DRAFT = PrDraft(commit_message="fix: x", pr_title="Fix the empty-span export", pr_body="Guards the empty case.")
MODELS = {"coder": "model-c", "reviewer": "model-r"}
VALIDATION = {"commands": [{"name": "unit", "exit_code": 0}]}


def row(conditions: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    vetting = {"ai_policy": "allowed_with_conditions" if conditions else "unstated", "ai_conditions": conditions or []}
    return {"id": "c_0000000001", "issue_number": 7, "vetting_json": json.dumps(vetting), **extra}


def build(c: dict[str, Any]) -> tuple[str, str]:
    conds = package.ai_conditions(c)
    body = package.pr_body(DRAFT, c, validation=VALIDATION, models=MODELS, login="test-bot-login", conditions=conds)
    return package.pr_title(DRAFT, conds), body


def test_no_conditions_body_is_unchanged() -> None:
    title, body = build(row())
    legacy = package.pr_body(DRAFT, row(), validation=VALIDATION, models=MODELS, login="test-bot-login")
    assert body == legacy and CHECKLIST_HEADER not in body and title == "Fix the empty-span export"
    assert "AI assistance disclosure" in body  # the disclosure is always there


def test_conditions_are_applied_and_verifiable() -> None:
    conds = [
        {"kind": "disclosure", "source": "CONTRIBUTING.md"}, {"kind": "human_review"},
        {"kind": "label", "value": "ai-assisted"}, {"kind": "title_tag", "value": "[AI]"},
        {"kind": "template_checkbox", "value": "I disclosed AI assistance"},
    ]  # fmt: skip
    title, body = build(row(conds))
    assert title == "[AI] Fix the empty-span export"
    assert CHECKLIST_HEADER in body and "- [x] A human (@test-bot-login) reviewed this change" in body
    assert "Please add the `ai-assisted` label" in body and "From the PR template: I disclosed AI assistance" in body
    assert body.index(CHECKLIST_HEADER) < body.index("AI assistance disclosure")  # checklist before disclosure
    parsed = tuple(AiCondition.model_validate(c) for c in conds)
    assert missing_conditions(title, body, parsed) == []
    again = package.pr_title(PrDraft(commit_message="x", pr_title=title, pr_body="b"), parsed)
    assert again.count("[AI]") == 1  # tagged once


def test_footer_is_one_line_without_tool_or_model_names() -> None:
    """§12.3 (owner, post-v1): the validation summary, then one <sub> line linking the project, then the marker."""
    _, body = build(row())
    assert body.splitlines()[-4:] == [
        "Validation run locally: 1/1 commands passed (unit).",
        "",
        f'<sub>🤖 AI assistance disclosure: prepared with <a href="{PROJECT_URL}">autonomous-oss-contributor</a>; '
        "reviewed and approved by @test-bot-login before submission.</sub>",
        "<!-- ossbot:cid=c_0000000001 -->",
    ]
    assert body.index("### How this was validated") < body.index("AI assistance disclosure")
    for absent in ("Kiro", "model-c", "model-r", "<details>", "\n---\n"):
        assert absent not in body, absent
    fork = package.pr_body(DRAFT, row(), validation=VALIDATION, models=MODELS, login="x",
                           project_url="https://example.org/fork")  # fmt: skip
    assert '<a href="https://example.org/fork">autonomous-oss-contributor</a>' in fork


def test_tool_named_condition_still_names_the_models() -> None:
    """A repo that requires naming the tool gets it in the checklist, never in the footer."""
    _, body = build(row([{"kind": "tool_named"}, {"kind": "human_review"}]))
    checklist = body[body.index(CHECKLIST_HEADER) : body.index("Validation run locally")]
    assert "Tools used: Kiro CLI (coder model `model-c`, reviewer model `model-r`)." in checklist
    assert "- [x] A human (@test-bot-login) reviewed this change" in checklist
    assert "Kiro" not in body[body.index("<sub>") :]


def test_invalid_stored_conditions_are_dropped() -> None:
    c = row([{"kind": "shell", "value": "rm -rf /"}, {"kind": "label", "value": "ok-label"}])
    assert [cond.kind for cond in package.ai_conditions(c)] == ["label"]
    assert package.ai_conditions({"vetting_json": "not json"}) == ()
    assert package.ai_conditions({"vetting_json": None}) == ()
