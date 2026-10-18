"""Task prompts for the Kiro roles (DESIGN §15.3) and the PR disclosure (§12.3).

The role's system prompt is the agent's ``prompt`` (``file:///opt/ossbot/prompts/<role>.md``, baked
into sandbox-llm). The runner writes the per-run task text (``<role>-task.md``) into the job's
input dir: the shared rules (``_common.md``), the task, and every piece of third-party text inside
``<untrusted>`` tags. The owner's own words go inside ``<human_instruction>``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

_REPO_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"
DEPLOYED_PROMPTS = Path("/opt/ossbot/app/prompts")


def prompts_dir() -> Path:
    override = os.environ.get("OSSBOT_PROMPTS_DIR")
    if override:
        return Path(override)
    return _REPO_PROMPTS if _REPO_PROMPTS.is_dir() else DEPLOYED_PROMPTS


def _env() -> SandboxedEnvironment:
    return SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)


def _neutral(text: str, tag: str) -> str:
    """Third-party text can't close (or open) our wrapper tags."""
    return text.replace(f"</{tag}", f"</{tag}_").replace(f"<{tag}", f"<{tag}_")


def untrusted(source: str, body: str, limit: int = 60_000) -> str:
    body = _neutral(_neutral(body[:limit], "untrusted"), "human_instruction")
    template = (prompts_dir() / "untrusted_wrapper.md").read_text(encoding="utf-8")
    return _env().from_string(template).render(source=source[:200], body=body)


def human(text: str, limit: int = 4000) -> str:
    body = _neutral(_neutral(text[:limit], "human_instruction"), "untrusted")
    return f"<human_instruction>\n{body}\n</human_instruction>"


def task(role: str, instruction: str, sections: list[str], schema_hint: str) -> str:
    common = (prompts_dir() / "_common.md").read_text(encoding="utf-8")
    parts = [common.rstrip(), f"## Task ({role})\n\n{instruction.strip()}", *sections,
             f"## Answer\n\nOne fenced ```json block matching the {role} schema:\n{schema_hint}"]  # fmt: skip
    return "\n\n".join(parts) + "\n"


def disclosure(**values: Any) -> str:
    template = (prompts_dir() / "pr_disclosure.md").read_text(encoding="utf-8")
    return _env().from_string(template).render(**values)
