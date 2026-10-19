"""The read-only GitHub MCP surface (phase2-build-plan addition; GHMCP-1..4).

``github-mcp-server`` v2.0.2 started by ``ossbot-github-mcp`` (``stdio --read-only --toolsets
repos,issues,pull_requests,actions --tools get_me``) lists exactly these tools, every one with
``readOnlyHint: true`` (listed live on 2026-10-19, ADR-0002 addendum, FEAT-007 round 3). The Kiro
scout and reviewer agents trust only these ids, and the gatekeeper's Hermes surface check treats any
other tool of the ``mcp-github`` toolset as drift. A version bump re-lists them here.
"""

from __future__ import annotations

SERVER_NAME = "github"
WRAPPER = "/opt/ossbot/bin/ossbot-github-mcp"
MCP_ROLES = frozenset({"scout", "reviewer"})  # research-profile Kiro roles that get the server and the read token
READ_TOOLS: tuple[str, ...] = (
    "actions_get", "actions_list", "get_commit", "get_file_contents", "get_job_logs", "get_label",
    "get_latest_release", "get_me", "get_release_by_tag", "get_tag", "issue_read", "list_branches",
    "list_commits", "list_issue_fields", "list_issue_types", "list_issues", "list_pull_requests", "list_releases",
    "list_repository_collaborators", "list_tags", "pull_request_read", "search_code", "search_commits",
    "search_issues", "search_pull_requests", "search_repositories",
)  # fmt: skip


def kiro_tool_ids() -> list[str]:
    """``@github/<tool>`` for the Kiro agents and ``--trust-tools``."""
    return [f"@{SERVER_NAME}/{t}" for t in READ_TOOLS]


__all__ = ["MCP_ROLES", "READ_TOOLS", "SERVER_NAME", "WRAPPER", "kiro_tool_ids"]
