"""Import contracts (DESIGN §18.1, I1): only ossbot.gatekeeper.submit may import github_writer;
untrusted-side components never import the gatekeeper."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from tests.conftest import REPO_ROOT

SRC = REPO_ROOT / "src" / "ossbot"
WRITER = "ossbot.gatekeeper.github_writer"


def _module(path: Path) -> str:
    rel = path.relative_to(SRC.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            out.update(f"{node.module}.{alias.name}" for alias in node.names)
    return out


def test_only_submit_imports_github_writer() -> None:
    importers = sorted(_module(p) for p in SRC.rglob("*.py") if any(i == WRITER for i in _imports(p)))
    assert importers == ["ossbot.gatekeeper.submit"]


def test_untrusted_side_never_imports_gatekeeper() -> None:
    for package in ("runner", "sandboxd", "sandbox_tools"):
        for path in (SRC / package).rglob("*.py"):
            assert not [i for i in _imports(path) if i.startswith("ossbot.gatekeeper")], path


def test_lint_imports_contracts_kept() -> None:
    exe = Path(sys.executable).parent / "lint-imports"
    proc = subprocess.run([str(exe), "--no-cache"], cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Only ossbot.gatekeeper.submit imports ossbot.gatekeeper.github_writer KEPT" in proc.stdout
    assert "0 broken" in proc.stdout
