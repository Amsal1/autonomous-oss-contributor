"""Pipeline model backends (ADR-0022). ``select_backend`` maps the ``config/models.yaml`` backend
(``common.backends``) to its implementation; ``kiro`` (Kiro CLI headless in sandboxd) is the only one."""

from __future__ import annotations

from ossbot.common.backends import UnknownBackend, backend_for, backend_named
from ossbot.common.config import ModelsConfig
from ossbot.runner.backends.base import ROLE_PROFILE, Check, LlmResult, ModelBackend, new_run_id
from ossbot.runner.backends.kiro_headless import KiroHeadlessBackend

__all__ = ["IMPLEMENTATIONS", "ROLE_PROFILE", "Check", "KiroHeadlessBackend", "LlmResult", "ModelBackend",
           "UnknownBackend", "new_run_id", "select_backend", "select_backend_named"]  # fmt: skip

IMPLEMENTATIONS: dict[str, type[KiroHeadlessBackend]] = {"kiro": KiroHeadlessBackend}


def select_backend_named(name: str) -> type[KiroHeadlessBackend]:
    spec = backend_named(name, "pipeline")  # UnknownBackend for an unregistered or assistant name
    impl = IMPLEMENTATIONS.get(spec.name)
    if impl is None:
        raise UnknownBackend(f"pipeline backend {spec.name!r} has no implementation in ossbot.runner.backends")
    return impl


def select_backend(cfg: ModelsConfig) -> type[KiroHeadlessBackend]:
    """The implementation of the pipeline backend ``cfg`` names (``backend``, default ``kiro``)."""
    return select_backend_named(backend_for(cfg).name)
