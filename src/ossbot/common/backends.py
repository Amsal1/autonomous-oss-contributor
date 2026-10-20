"""Model backends: what each one needs from the fixed sandbox, probe and key-scan boundary (ADR-0022).

Pure data, no component imports. A backend is how a role reaches a model; it never changes the
foundations around it (gatekeeper, approvals, state machine, sandbox profiles, egress, Docker filter,
probes, key-scan, DESIGN §8.3 I1-I13). Two kinds:

* ``pipeline``: headless role jobs that sandboxd runs (``runner.backends``); today ``kiro``
  (``kiro-cli chat`` in the ``kiro`` image entrypoint, the catalog from ``list_models``).
* ``assistant``: the Hermes provider plugin; today ``kiro-acp`` (``vendor/hermes-kiro-provider``,
  Kiro CLI V3 ACP, probed by ``ossctl hermes probe``). Nothing in the pipeline calls it.

``config/models.yaml`` ``backend`` names the pipeline backend (default ``kiro``); the schema accepts
only registered names, so an unknown one is refused like any other invalid models.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ossbot.common.config import ModelsConfig

BackendKind = Literal["pipeline", "assistant"]

# sandboxd.probes.PROBES: every pipeline backend passes all of them for its exact
# CLI/image/agent-config/settings/trust-mode tuple (tests/unit/sandboxd/test_backend_surface.py).
PIPELINE_PROBES: tuple[str, ...] = ("research_deny", "symlink", "coder_surface", "settings", "workspace")
# ctl.hermes_probe: tool surface, deny and exfil checks of the assistant session (ADR-0018).
ASSISTANT_PROBES: tuple[str, ...] = ("hermes_probe",)
KIRO_KEY_FORMAT = r"^ksk_[A-Za-z0-9_\-]{16,}$"  # §18.3 entry validation (setup-secrets.sh, ossctl doctor)


@dataclass(frozen=True)
class BackendSpec:
    name: str
    kind: BackendKind
    # image entrypoint of a role job and of the catalog job (pipeline only)
    sandbox_entrypoint: str | None
    catalog_entrypoint: str | None
    # sandboxd profiles whose ``sandbox_entrypoint`` job gets the credential (name-only --env)
    key_profiles: tuple[str, ...]
    # env names whose values sandboxd key-scans in every output and log (exact value + encodings)
    credential_env: tuple[str, ...]
    # format regexes of those credentials (doctor and setup checks; not used by the output scan)
    credential_patterns: tuple[str, ...]
    required_probes: tuple[str, ...]
    hermes_provider: str | None  # the Hermes provider plugin name (assistant only)

    def key_pairs(self) -> frozenset[tuple[str, str]]:
        """``(profile, entrypoint)`` pairs that get the credential; must be a subset of the Docker
        filter's ``KEY_PAIRS`` (a new backend needs a reviewed filter change)."""
        pairs = {(p, self.sandbox_entrypoint) for p in self.key_profiles if self.sandbox_entrypoint}
        if self.catalog_entrypoint:
            pairs.add(("catalog", self.catalog_entrypoint))
        return frozenset(pairs)


KIRO_HEADLESS = BackendSpec(
    name="kiro",
    kind="pipeline",
    sandbox_entrypoint="kiro",
    catalog_entrypoint="list_models",
    key_profiles=("research", "agent-llm"),
    credential_env=("KIRO_API_KEY",),
    credential_patterns=(KIRO_KEY_FORMAT,),
    required_probes=PIPELINE_PROBES,
    hermes_provider=None,
)
KIRO_ACP = BackendSpec(
    name="kiro-acp",
    kind="assistant",
    sandbox_entrypoint=None,
    catalog_entrypoint=None,
    key_profiles=(),
    credential_env=("KIRO_API_KEY",),  # hermes.env, read by uid hermes only
    credential_patterns=(KIRO_KEY_FORMAT,),
    required_probes=ASSISTANT_PROBES,
    hermes_provider="kiro-acp",
)
BACKENDS: dict[str, BackendSpec] = {KIRO_HEADLESS.name: KIRO_HEADLESS, KIRO_ACP.name: KIRO_ACP}
DEFAULT_BACKEND = KIRO_HEADLESS.name
ASSISTANT_BACKEND = KIRO_ACP.name  # fixed: the Hermes config names its provider (hermes/config/config.yaml.j2)


class UnknownBackend(ValueError):
    """A backend name that is not registered, or not of the kind asked for."""


def backend_named(name: str, kind: BackendKind) -> BackendSpec:
    spec = BACKENDS.get(name)
    if spec is None or spec.kind != kind:
        raise UnknownBackend(f"unknown {kind} backend {name!r} (registered: {', '.join(sorted(BACKENDS))})")
    return spec


def backend_for(cfg: ModelsConfig) -> BackendSpec:
    """The pipeline backend ``config/models.yaml`` selects (``backend``, default ``kiro``)."""
    return backend_named(cfg.backend, "pipeline")


def pipeline_backends() -> tuple[BackendSpec, ...]:
    return tuple(s for s in BACKENDS.values() if s.kind == "pipeline")


def pipeline_credential_env() -> tuple[str, ...]:
    """Credential env names of every registered pipeline backend, in registry order, no repeats: the
    names whose values sandboxd key-scans (it holds the pipeline keys, whichever backend runs)."""
    names: list[str] = []
    for spec in pipeline_backends():
        names += [n for n in spec.credential_env if n not in names]
    return tuple(names)
