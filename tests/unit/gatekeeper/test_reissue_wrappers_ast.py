"""BA-1 / BA-17 (DESIGN §14.18.4.3): only the AppContext wrappers call a decision service's
``reissue``; the services never publish (no ``on_approval_created`` in their modules)."""

from __future__ import annotations

import ast

from tests.conftest import REPO_ROOT

SRC = REPO_ROOT / "src" / "ossbot"
SERVICES = ("approvals.py", "proposal_approvals.py", "skill_approvals.py", "decisions.py")


def _calls(tree: ast.AST, attr: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == attr]  # fmt: skip


def _enclosing(tree: ast.AST) -> dict[ast.AST, str]:
    out: dict[ast.AST, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            for node in ast.walk(fn):
                out.setdefault(node, fn.name)
    return out


# Receivers that are an AppContext in the Telegram layer: calling the wrapper itself is the point.
APP_CONTEXT_RECEIVERS = frozenset({"ctx", "app", "tap.app", "self.ctx"})


def test_only_the_context_wrappers_call_reissue() -> None:
    callers: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = _enclosing(tree)
        rel = path.relative_to(SRC).as_posix()
        for call in _calls(tree, "reissue"):
            assert isinstance(call.func, ast.Attribute)
            if rel.startswith("gatekeeper/telegram/") and ast.unparse(call.func.value) in APP_CONTEXT_RECEIVERS:
                continue  # AppContext.reissue (the wrapper), not a decision service's
            callers.append(f"{rel}:{owner.get(call, '<module>')}")
    allowed = {
        "gatekeeper/context.py:reissue",
        "gatekeeper/context.py:reissue_proposal",
        "gatekeeper/context.py:reissue_skill",
        # the reload re-issue of an open proposal/skill goes through the service's own reissue
        "gatekeeper/proposal_approvals.py:reissue_invalidated",
        "gatekeeper/skill_approvals.py:reissue_invalidated",
    }
    assert set(callers) <= allowed, sorted(set(callers) - allowed)
    assert {"gatekeeper/context.py:reissue", "gatekeeper/context.py:reissue_proposal",
            "gatekeeper/context.py:reissue_skill"} <= set(callers)  # fmt: skip


def test_reissue_invalidated_is_called_only_from_reissue_cards() -> None:
    callers = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = _enclosing(tree)
        for call in _calls(tree, "reissue_invalidated"):
            callers.add(f"{path.relative_to(SRC).as_posix()}:{owner.get(call, '<module>')}")
    assert callers == {"gatekeeper/context.py:reissue_cards"}


def test_services_never_publish() -> None:
    for name in SERVICES:
        text = (SRC / "gatekeeper" / name).read_text(encoding="utf-8")
        assert "on_approval_created" not in text, name
