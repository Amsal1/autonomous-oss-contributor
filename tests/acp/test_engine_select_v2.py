"""Engine selection (DESIGN §15.2): ``HERMES_KIRO_ACP_ENGINE=v3`` (default) builds the V3 client; ``v2``
keeps the untouched upstream path; ``kiro_acp/__init__.py`` and ``v3_core`` import nothing from Hermes."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from tests.acp.conftest import VENDOR

UPSTREAM_COMMIT = "02c4dd06585436751145961658a22b773f23a0ef"
# sha256 of the files as imported unmodified from the upstream archive: the V2 fallback stays untouched.
UPSTREAM_V2 = {
    "kiro_acp_client.py": "6afc67318d3870c956d3ea3ea4e1de20dae8fbf000b7a85e9d89162539a27d2a",
    "kiro_acp_models.py": "6156770ad5686f4a99322f85fd94feb9c88f561a39d7e45d60ef3967ccbc3e9b",
    "LICENSE": "f7208d5ea6250de9495f85823f8a1a4387b8d91e4e6b9cc7fd5cbd14f29b93fc",
}


def _load_plugin(monkeypatch: pytest.MonkeyPatch) -> tuple[types.ModuleType, list[Any]]:
    registered: list[Any] = []
    providers = types.ModuleType("providers")
    providers.register_provider = registered.append  # type: ignore[attr-defined]
    base = types.ModuleType("providers.base")

    class ProviderProfile:
        def __init__(self, **kw: Any) -> None:
            for k, v in kw.items():
                setattr(self, k, v)

    base.ProviderProfile = ProviderProfile  # type: ignore[attr-defined]
    agent = types.ModuleType("agent")
    shim = types.ModuleType("agent.copilot_acp_client")

    class CopilotACPClient:
        def __init__(self, **kw: Any) -> None:
            self.kw = kw

    shim.CopilotACPClient = CopilotACPClient  # type: ignore[attr-defined]
    shim.ACP_MARKER_BASE_URL = "acp://copilot"  # type: ignore[attr-defined]
    for name, mod in {
        "providers": providers,
        "providers.base": base,
        "agent": agent,
        "agent.copilot_acp_client": shim,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location(
        "kiro_acp_plugin_under_test", VENDOR / "__init__.py", submodule_search_locations=[str(VENDOR)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module, registered


def test_default_engine_is_v3(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HERMES_KIRO_ACP_ENGINE", raising=False)
    _plugin, registered = _load_plugin(monkeypatch)
    assert [p.name for p in registered] == ["kiro-acp"]
    client = registered[0].create_client(command="kiro-cli", args=("acp",))
    assert type(client).__name__ == "KiroV3Client"
    monkeypatch.setenv("HERMES_KIRO_ACP_ENGINE", "bogus")  # unknown -> v3
    assert type(registered[0].create_client()).__name__ == "KiroV3Client"


def test_v2_engine_keeps_the_upstream_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KIRO_ACP_ENGINE", "v2")
    _plugin, registered = _load_plugin(monkeypatch)
    client = registered[0].create_client(command="kiro-cli", args=("acp",))
    assert type(client).__name__ == "KiroACPClient"
    assert client.kw["acp_args"] == ["acp"]


def test_v2_files_are_byte_identical_to_the_upstream_import() -> None:
    assert (VENDOR / "UPSTREAM_COMMIT").read_text(encoding="utf-8").strip() == UPSTREAM_COMMIT
    for name, digest in UPSTREAM_V2.items():
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == digest, name


@pytest.mark.parametrize("name", ["__init__.py", "v3_core.py"])
def test_core_imports_only_the_stdlib(name: str) -> None:
    tree = ast.parse((VENDOR / "kiro_acp" / name).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports in the stdlib-only core"
            imported.add((node.module or "").split(".")[0])
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}
    if name == "__init__.py":
        assert imported == set()
