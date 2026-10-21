"""The repo docs that state an AI-contribution policy (DESIGN §9.5), read through the GET-only client.

Takes the read client, not a StageContext, so the vet stage and the read-only ``ossctl targets facts``
share it. The texts are untrusted: they are only classified (``common.ai_policy``) and quoted to the
vetter inside untrusted sections.
"""

from __future__ import annotations

from typing import Any

from ossbot.common.ai_policy import MAINTAINER_ASSOCIATIONS, POLICY_DOC_GROUPS, PolicyDoc
from ossbot.runner.github_read import GitHubRead

README = "README"


def fetch_policy_docs(gh: GitHubRead, repo: str) -> dict[str, PolicyDoc]:
    """group -> the doc found (contributing, ai_policy, code_of_conduct, pr_template, support, readme)."""
    out: dict[str, PolicyDoc] = {}
    for group, paths in POLICY_DOC_GROUPS.items():
        for path in paths:
            text = gh.file_text(repo, path)
            if text:
                out[group] = PolicyDoc(path=path, text=text)
                break
    readme = gh.readme(repo)
    if readme:
        out["readme"] = PolicyDoc(path=README, text=readme)
    return out


def maintainer_docs(repo: str, number: int | None, issue: dict[str, Any] | None,
                    comments: list[dict[str, Any]]) -> list[PolicyDoc]:  # fmt: skip
    """The issue body and comments as docs; ``maintainer`` from ``author_association`` (OWNER, MEMBER,
    COLLABORATOR). The classifier uses only maintainer texts, and only for bans."""
    out: list[PolicyDoc] = []
    ref = f"{repo}#{number}"
    if issue:
        assoc = str(issue.get("author_association") or "")
        out.append(PolicyDoc(path=f"{ref} issue", text=str(issue.get("body") or ""), kind="issue",
                             maintainer=assoc in MAINTAINER_ASSOCIATIONS))  # fmt: skip
    for cm in comments:
        assoc = str(cm.get("author_association") or "")
        out.append(PolicyDoc(path=f"{ref} comment", text=str(cm.get("body") or ""), kind="issue",
                             maintainer=assoc in MAINTAINER_ASSOCIATIONS))  # fmt: skip
    return out
