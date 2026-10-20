"""Model backend registry and config selection (ADR-0022)."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from ossbot.common.backends import (
    ASSISTANT_BACKEND,
    BACKENDS,
    DEFAULT_BACKEND,
    KIRO_ACP,
    KIRO_HEADLESS,
    UnknownBackend,
    backend_for,
    backend_named,
    pipeline_backends,
    pipeline_credential_env,
)
from ossbot.common.config import ConfigError, parse_models

REPO = Path(__file__).resolve().parents[3]
SHIPPED = (REPO / "config" / "models.yaml").read_bytes()
# The shipped file sets `backend: kiro`; the cases below start from the file without that line.
MODELS = re.sub(rb"(?m)^backend:[^\n]*\n", b"", SHIPPED)


def _with_backend(line: str) -> bytes:
    return MODELS.replace(b"\nversion: 1\n", f"\nversion: 1\n{line}\n".encode(), 1)


def test_shipped_file_names_kiro() -> None:
    assert re.search(rb"(?m)^backend: kiro\b", SHIPPED) and b"backend:" not in MODELS
    assert parse_models(SHIPPED).backend == "kiro" and backend_for(parse_models(SHIPPED)) is KIRO_HEADLESS


def test_registry() -> None:
    assert BACKENDS == {"kiro": KIRO_HEADLESS, "kiro-acp": KIRO_ACP}
    assert DEFAULT_BACKEND == "kiro" and ASSISTANT_BACKEND == "kiro-acp"
    assert pipeline_backends() == (KIRO_HEADLESS,)
    assert KIRO_HEADLESS.kind == "pipeline" and KIRO_HEADLESS.hermes_provider is None
    assert (KIRO_HEADLESS.sandbox_entrypoint, KIRO_HEADLESS.catalog_entrypoint) == ("kiro", "list_models")
    assert KIRO_ACP.kind == "assistant" and KIRO_ACP.hermes_provider == "kiro-acp"
    assert KIRO_ACP.sandbox_entrypoint is None and KIRO_ACP.key_pairs() == frozenset()


def test_kiro_key_pairs_and_credentials_are_todays() -> None:
    assert KIRO_HEADLESS.key_pairs() == {("research", "kiro"), ("agent-llm", "kiro"), ("catalog", "list_models")}
    assert KIRO_HEADLESS.credential_env == ("KIRO_API_KEY",)
    assert pipeline_credential_env() == ("KIRO_API_KEY",)
    (pattern,) = KIRO_HEADLESS.credential_patterns
    assert pattern == r"^ksk_[A-Za-z0-9_\-]{16,}$"
    assert re.fullmatch(pattern, "ksk_" + "a" * 16) and not re.fullmatch(pattern, "ghp_" + "a" * 20)


def test_backend_for_defaults_to_kiro_and_accepts_it_explicitly() -> None:
    assert parse_models(MODELS).backend == "kiro"
    assert backend_for(parse_models(MODELS)) is KIRO_HEADLESS
    explicit = parse_models(_with_backend("backend: kiro"))
    assert explicit.backend == "kiro" and backend_for(explicit) is KIRO_HEADLESS
    assert explicit.roles == parse_models(MODELS).roles  # the key selects the backend only


@pytest.mark.parametrize("name", ["claude", "codex", "openai", "kiro-acp", "Kiro", "''"])
def test_unknown_backend_is_refused_by_the_schema(name: str) -> None:
    with pytest.raises(ConfigError, match="backend"):
        parse_models(_with_backend(f"backend: {name}"))


def test_backend_named_refuses_unregistered_and_wrong_kind() -> None:
    assert backend_named("kiro-acp", "assistant") is KIRO_ACP
    with pytest.raises(UnknownBackend):
        backend_named("kiro-acp", "pipeline")
    with pytest.raises(UnknownBackend):
        backend_named("kiro", "assistant")
    with pytest.raises(UnknownBackend, match="registered: kiro, kiro-acp"):
        backend_named("claude", "pipeline")


def test_specs_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        KIRO_HEADLESS.credential_env = ("OTHER",)  # type: ignore[misc]
