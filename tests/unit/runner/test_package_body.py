"""The PR body and title apply the repo's recorded AI-policy conditions (DESIGN §9.5, §9.11, post-v1)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ossbot.common.ai_policy import CHECKLIST_HEADER, AiCondition, missing_conditions
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


def test_invalid_stored_conditions_are_dropped() -> None:
    c = row([{"kind": "shell", "value": "rm -rf /"}, {"kind": "label", "value": "ok-label"}])
    assert [cond.kind for cond in package.ai_conditions(c)] == ["label"]
    assert package.ai_conditions({"vetting_json": "not json"}) == ()
    assert package.ai_conditions({"vetting_json": None}) == ()
