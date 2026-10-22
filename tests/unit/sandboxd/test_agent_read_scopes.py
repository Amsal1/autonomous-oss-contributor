"""Read-only repo scopes of the Kiro roles (DESIGN §9.5a, §15.3; ADR-0011 addendum "scout repo read").

The scout (and the planner and reviewer) may list and read ``/work/repo`` and below, and nothing they
could write, execute or use to reach secrets; the vetter stays repo-less (owner decision C,
2026-10-22). No agent JSON was changed for this: these tests pin the existing scopes so they can't
drift. ``kas_fs_match`` models how kiro-cli 2.32.0 (KAS bundle 2.32.0-f3c56062…,
``matchExpandedPattern``) matches an ``fs_read`` path against a glob.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from ossbot.common.github_mcp_tools import MCP_ROLES, kiro_tool_ids
from ossbot.sandboxd import profiles
from ossbot.sandboxd.argv import job_spec

ROOT = Path(__file__).resolve().parents[3]
AGENTS = ROOT / ".kiro" / "agents"
REPO_ROLES = ("scout", "planner", "reviewer")
REPO_SCOPE = ["/work/repo/**", "/io/input/**"]
ALLOWED_TOOLS = {"read", "web_search", "web_fetch", *kiro_tool_ids()}
FORBIDDEN_TOOLS = {"write", "shell", "glob", "grep", "execute_bash", "fs_write"}
MUST_DENY = ["/home/agent/.kiro/**", "/etc/**", "/proc/**", "/run/**", "/io/output/**"]
OUTSIDE = ["/work/repo2", "/work/repo2/x", "/work", "/work/cache/x", "/home/agent/.kiro/sessions/x/tool-outputs/a.txt",
           "/home/agent/.ossbot/github-token", "/etc/passwd", "/proc/self/environ"]  # fmt: skip


def _glob_re(pattern: str) -> re.Pattern[str]:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def kas_fs_match(path: str, pattern: str) -> bool:
    """KAS 2.32.0 ``matchExpandedPattern(t, r)`` for absolute paths: ``minimatch`` (``**`` crosses
    ``/``, ``*`` stays within one component) || ``matchesDirectoryRoot`` (``<dir>/**`` also matches
    ``<dir>`` itself, which is what lets ``list_directory /work/repo`` through) ||
    ``matchesImplicitSubtree`` (a pattern without wildcards also matches its children)."""
    if _glob_re(pattern).match(path):
        return True
    if pattern.endswith("/**") and path == pattern[:-3]:
        return True
    return not any(c in pattern for c in "*?[") and (path == pattern or path.startswith(pattern.rstrip("/") + "/"))


def covered(path: str, patterns: list[str]) -> bool:
    return any(kas_fs_match(path, p) for p in patterns)


def agent(role: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((AGENTS / f"ossbot-{role}.json").read_text(encoding="utf-8"))
    return doc


def _rules(doc: dict[str, Any], capability: str, effect: str) -> list[dict[str, Any]]:
    return [r for r in doc["permissions"]["rules"] if r.get("capability") == capability and r.get("effect") == effect]


def test_the_matcher_model() -> None:
    assert kas_fs_match("/work/repo", "/work/repo/**")  # matchesDirectoryRoot
    assert kas_fs_match("/work/repo/a/b/c.go", "/work/repo/**")
    assert not kas_fs_match("/work/repo2", "/work/repo/**")
    assert kas_fs_match("/work/repo/a.go", "/work/repo")  # matchesImplicitSubtree
    assert kas_fs_match("/work/repo/a.go", "/work/repo/*") and not kas_fs_match("/work/repo/a/b.go", "/work/repo/*")


@pytest.mark.parametrize("role", REPO_ROLES)
def test_repo_roles_list_and_read_the_snapshot_only(role: str) -> None:
    doc = agent(role)
    allowed = doc["toolsSettings"]["read"]["allowedPaths"]
    [allow] = _rules(doc, "fs_read", "allow")
    assert allowed == allow["match"] == REPO_SCOPE
    for path in ("/work/repo", "/work/repo/README.md", "/work/repo/a/b.go", "/io/input/issue.md"):
        assert covered(path, allowed), path  # /work/repo itself: list_directory on the root
    for path in OUTSIDE:
        assert not covered(path, allowed), path
    assert profiles.ROLE_REPO_MODE[role] == "snapshot"


@pytest.mark.parametrize("role", [*REPO_ROLES, "vetter"])
def test_no_write_shell_or_secret_paths(role: str) -> None:
    doc = agent(role)
    assert set(doc["tools"]) <= ALLOWED_TOOLS and not set(doc["tools"]) & FORBIDDEN_TOOLS
    assert doc["tools"] == doc["allowedTools"]
    assert "write" not in doc["toolsSettings"] and "shell" not in doc["toolsSettings"]
    for cap in ("fs_write", "shell", "subagent"):
        assert not _rules(doc, cap, "allow"), cap
    denied = doc["toolsSettings"]["read"]["deniedPaths"]
    [deny] = _rules(doc, "fs_read", "deny")
    assert deny["match"] == denied
    expected = [*MUST_DENY, "/home/agent/.ossbot/**"] if role in MCP_ROLES else MUST_DENY
    assert set(expected) <= set(denied)
    for path in ("/home/agent/.kiro/sessions/x/tool-outputs/a.txt", "/etc/passwd", "/io/output/result.json"):
        assert covered(path, denied), path  # deny > allow in every scope
    if role in MCP_ROLES:
        assert covered("/home/agent/.ossbot/github-token", denied)
    assert not [r for r in doc["permissions"]["rules"] if "exclude" in r]


def test_the_vetter_stays_repo_less() -> None:
    doc = agent("vetter")
    assert doc["toolsSettings"]["read"]["allowedPaths"] == ["/io/input/**"]
    assert _rules(doc, "fs_read", "allow") == [{"capability": "fs_read", "match": ["/io/input/**"], "effect": "allow"}]
    assert not covered("/work/repo", ["/io/input/**"]) and not covered("/work/repo/a.go", ["/io/input/**"])
    assert profiles.ROLE_REPO_MODE["vetter"] == "none"


def test_trust_tools_unchanged() -> None:
    assert profiles.TRUST_TOOLS["vetter"] == "read,web_search,web_fetch"
    assert profiles.TRUST_TOOLS["scout"] == ",".join(["read", "web_search", "web_fetch", *kiro_tool_ids()])


def test_the_key_holding_scout_sees_the_snapshot_read_only() -> None:
    """I5: the research container holds the Kiro key, so its only repo mount is the read-only snapshot."""
    spec = job_spec("research", run_id="scout-1", cid="c_01ABCDEFGH", entrypoint="kiro",
                    image_ref="ossbot/sandbox-llm@sha256:" + "0" * 64, network="ossbot-t-x", repo_mode="snapshot",
                    env={"ROLE": "scout"})  # fmt: skip
    repo = [m for m in spec.mounts if m.target.startswith("/work")]
    assert [(m.target, m.read_only) for m in repo] == [("/work/repo", True)]
    assert repo[0].source.startswith("ossbot-ro-")
