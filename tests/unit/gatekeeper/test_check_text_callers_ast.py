"""BA-21: every production call of ``policy.check_text`` passes ``secret_values=`` (U14)."""

from __future__ import annotations

import ast

from tests.conftest import REPO_ROOT

SRC = REPO_ROOT / "src" / "ossbot"


def test_every_check_text_call_passes_secret_values() -> None:
    calls: list[str] = []
    missing: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name != "check_text":
                continue
            where = f"{path.relative_to(SRC).as_posix()}:{node.lineno}"
            calls.append(where)
            if not any(k.arg == "secret_values" for k in node.keywords):
                missing.append(where)
    assert calls, "no check_text call found"
    assert missing == []
