"""The pipeline model-backend interface (ADR-0022).

A backend runs one role inside the existing sandbox and probe boundary: it resolves the role's model
from ``config/models.yaml`` against the gatekeeper's catalog, submits a sandboxd job (the profile comes
from ``ROLE_PROFILE``, never from the backend), and turns the job's artifacts into a validated answer.
It never holds a credential, never talks to a model directly and never reaches the gatekeeper's
write paths (import-linter).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from ossbot.common.backends import BackendSpec
from ossbot.common.models import ResolvedModel

ROLE_PROFILE: dict[str, tuple[str, str]] = {  # role -> (sandboxd profile, repo_mode)
    "vetter": ("research", "none"), "scout": ("research", "snapshot"), "planner": ("research", "snapshot"),
    "reviewer": ("research", "snapshot"), "summarizer": ("research", "none"), "coder": ("coder", "rw"),
}  # fmt: skip
Check = Callable[[Any], str | None]  # deterministic check of a parsed answer: None = fine, else the reason


def new_run_id(stage: str) -> str:
    return f"{stage[:10]}-{secrets.token_hex(6)}"


@dataclass
class LlmResult[T: BaseModel]:
    value: T
    model: ResolvedModel
    model_actual: str | None
    model_mismatch: bool
    run_ids: list[str] = field(default_factory=list)
    artifact_dir: Path | None = None


@runtime_checkable
class ModelBackend(Protocol):
    """``run_role`` -> a validated result (or ``StageFailed``/``Interrupted``); ``list_models`` -> the
    gatekeeper's catalog rows the backend resolves against; ``probe_surface`` -> what the backend must
    pass and declare (probes, credential names, key pairs)."""

    def run_role[T: BaseModel](
        self,
        cid: str,
        role: str,
        out_model: type[T],
        text: str,
        files: Mapping[str, str | bytes] | None = None,
        *,
        timeout_s: int = 1200,
        schema_hint: str = "",
        check: Check | None = None,
    ) -> LlmResult[T]: ...

    def resolve(self, role: str) -> ResolvedModel: ...

    def list_models(self) -> list[dict[str, Any]]: ...

    def probe_surface(self) -> BackendSpec: ...
