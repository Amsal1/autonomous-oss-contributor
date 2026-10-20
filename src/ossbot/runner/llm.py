"""One role invocation through sandboxd (DESIGN §15.3, §16.1).

The implementation lives in ``runner.backends`` (ADR-0022): ``Kiro`` is the ``kiro`` pipeline backend
(``backends.kiro_headless.KiroHeadlessBackend``); the names below stay importable from here for the
stages and tests.
"""

from __future__ import annotations

from ossbot.runner import prompts
from ossbot.runner.backends.base import ROLE_PROFILE, Check, LlmResult, new_run_id
from ossbot.runner.backends.kiro_headless import KiroHeadlessBackend
from ossbot.runner.errors import StageFailed

__all__ = ["ROLE_PROFILE", "Check", "Kiro", "LlmResult", "StageFailed", "as_prompt", "new_run_id"]
Kiro = KiroHeadlessBackend


def as_prompt(role: str, instruction: str, sections: list[str], schema_hint: str) -> str:
    return prompts.task(role, instruction, sections, schema_hint)
