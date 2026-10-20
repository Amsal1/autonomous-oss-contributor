"""Import contracts (DESIGN §18.1, I1): only ossbot.gatekeeper.submit may import github_writer;
untrusted-side components never import the gatekeeper."""

from __future__ import annotations

import ast
import os
import re
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


def test_only_review_mirror_imports_its_writer() -> None:
    """AC 26 (ADR-0019 C, U5): only review_mirror imports review_mirror_writer."""
    name = "ossbot.gatekeeper.review_mirror_writer"
    importers = sorted(_module(p) for p in SRC.rglob("*.py") if any(i == name for i in _imports(p)))
    assert importers == ["ossbot.gatekeeper.review_mirror"]


def test_only_telegraph_imports_its_writer() -> None:
    """AC 26 (§14.12.8, U11): only telegraph imports telegraph_writer."""
    name = "ossbot.gatekeeper.telegraph_writer"
    importers = sorted(_module(p) for p in SRC.rglob("*.py") if any(i == name for i in _imports(p)))
    assert importers == ["ossbot.gatekeeper.telegraph"]


def test_untrusted_side_never_imports_gatekeeper() -> None:
    for package in ("runner", "sandboxd", "sandbox_tools"):
        for path in (SRC / package).rglob("*.py"):
            assert not [i for i in _imports(path) if i.startswith("ossbot.gatekeeper")], path


def test_lint_imports_contracts_kept() -> None:
    exe = Path(sys.executable).parent / "lint-imports"
    # Without a TTY (CI) import-linter's rich output wraps at 80 columns and splits long contract
    # lines; ask for a wide plain console and also match on whitespace-normalised text.
    env = {**os.environ, "COLUMNS": "250", "TERM": "dumb", "NO_COLOR": "1"}
    proc = subprocess.run([str(exe), "--no-cache"], cwd=REPO_ROOT, capture_output=True, text=True, check=False, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = " ".join(proc.stdout.split())
    linter = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").split("[tool.importlinter]", 1)[1]
    names = re.findall(r'^name = "([^"]+)"', linter, re.M)
    assert "Only ossbot.gatekeeper.review_mirror imports ossbot.gatekeeper.review_mirror_writer" in names
    for name in names:  # every contract of pyproject, each reported KEPT
        assert f"{name} KEPT" in out, name
    assert f"Contracts: {len(names)} kept, 0 broken" in out
