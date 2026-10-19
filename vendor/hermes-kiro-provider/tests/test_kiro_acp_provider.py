"""Behavior-contract tests for the kiro-acp provider plugin.

Two layers:
- Profile/client contracts (pure, no network): registration shape, env
  override plumbing, error translation.
- Real-discovery contract: the plugin registers when dropped into an isolated
  ``$HERMES_HOME/plugins/model-providers/kiro-acp/`` — the exact path real
  users install to. No live kiro-cli process is spawned by these tests.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_plugin(monkeypatch, hermes_home: Path | None = None):
    """Import the plugin exactly as Hermes' _import_plugin_dir does for user
    plugins (spec_from_file_location + submodule_search_locations), so the
    test exercises the real load path, including relative imports."""
    import importlib.util

    if hermes_home is not None:
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    module_name = "_hermes_user_provider_kiro_acp_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name, REPO_ROOT / "__init__.py",
        submodule_search_locations=[str(REPO_ROOT)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestProfileContract:
    def test_registers_kiro_acp_with_external_process_auth(self, monkeypatch):
        import providers
        mod = _load_plugin(monkeypatch)
        # The module-level register_provider call in __init__ targets Hermes'
        # registry; simulate what discovery produces.
        names = [p.name for p in providers.list_providers()]
        mod.register_provider(mod.kiro_acp)
        profile = providers.get_provider_profile("kiro-acp")
        assert profile is not None
        assert profile.auth_type == "external_process"
        assert profile.process_command == "kiro-cli"
        assert profile.process_args == ("acp",)
        assert "kiro" in profile.aliases

    def test_env_overrides_replace_command_and_args(self, monkeypatch):
        mod = _load_plugin(monkeypatch)
        monkeypatch.setenv("HERMES_KIRO_ACP_COMMAND", "/opt/custom/kiro")
        monkeypatch.setenv("HERMES_KIRO_ACP_ARGS", "acp --agent builder --effort high")
        assert mod.resolve_kiro_command() == "/opt/custom/kiro"
        assert mod.resolve_kiro_args() == ["acp", "--agent", "builder", "--effort", "high"]

    def test_defaults_without_env(self, monkeypatch):
        mod = _load_plugin(monkeypatch)
        monkeypatch.delenv("HERMES_KIRO_ACP_COMMAND", raising=False)
        monkeypatch.delenv("KIRO_CLI_PATH", raising=False)
        monkeypatch.delenv("HERMES_KIRO_ACP_ARGS", raising=False)
        assert mod.resolve_kiro_command() == "kiro-cli"
        assert mod.resolve_kiro_args() == ["acp"]

    def test_create_client_returns_kiro_client_with_profile_defaults(self, monkeypatch):
        mod = _load_plugin(monkeypatch)
        client = mod.kiro_acp.create_client()
        assert type(client).__name__ == "KiroACPClient"
        assert client._acp_command == "kiro-cli"
        assert client._acp_args == ["acp"]
        assert client.base_url == "acp://kiro"


class TestRealDiscovery:
    def test_plugin_dir_discovered_from_isolated_hermes_home(self, tmp_path, monkeypatch):
        """The install path real users use: drop-in under
        $HERMES_HOME/plugins/model-providers/kiro-acp/ must register."""
        import providers

        home = tmp_path / "hermes-home"
        target = home / "plugins" / "model-providers" / "kiro-acp"
        target.mkdir(parents=True)
        for fname in ("__init__.py", "kiro_acp_client.py", "plugin.yaml"):
            (target / fname).write_bytes((REPO_ROOT / fname).read_bytes())

        # Reset discovery state exactly like a fresh process.
        monkeypatch.setattr(providers, "_discovered", False)
        providers._REGISTRY.pop("kiro-acp", None)
        for alias in ("kiro", "kiro-cli", "kiro-acp-agent"):
            providers._ALIASES.pop(alias, None)

        monkeypatch.setenv("HERMES_HOME", str(home))
        profile = providers.get_provider_profile("kiro-acp")

        assert profile is not None, "kiro-acp must be discovered from $HERMES_HOME drop-in"
        assert profile.auth_type == "external_process"
        assert get_provider_profile_by_alias(providers, "kiro") is profile


def get_provider_profile_by_alias(providers, alias: str):
    return providers.get_provider_profile(alias)
