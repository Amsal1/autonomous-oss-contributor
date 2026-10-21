"""The policy docs vet reads (DESIGN §9.5, post-v1): first hit per group, README, maintainer flag."""

from __future__ import annotations

from typing import cast

import pytest

from ossbot.runner.github_read import GitHubRead
from ossbot.runner.repo_docs import README, fetch_policy_docs, maintainer_docs

pytestmark = pytest.mark.unit


class StubRead:
    """The two GET calls repo_docs uses; records which paths were asked for."""

    def __init__(self, files: dict[str, str], readme: str | None) -> None:
        self.files = files
        self.readme_text = readme
        self.asked: list[str] = []

    def file_text(self, repo: str, path: str) -> str | None:
        self.asked.append(path)
        return self.files.get(path)

    def readme(self, repo: str) -> str | None:
        return self.readme_text


def test_first_hit_per_group_and_readme() -> None:
    stub = StubRead(
        {
            ".github/CONTRIBUTING.md": "second contributing",
            "docs/CONTRIBUTING.md": "third contributing",
            ".github/pull_request_template.md": "- [ ] tests",
        },
        "# Widgets",
    )
    docs = fetch_policy_docs(cast(GitHubRead, stub), "acme/widgets")
    assert set(docs) == {"contributing", "pr_template", "readme"}
    assert docs["contributing"].path == ".github/CONTRIBUTING.md"
    assert "docs/CONTRIBUTING.md" not in stub.asked  # stops at the first hit
    assert docs["readme"].path == README and docs["readme"].text == "# Widgets"
    assert all(d.kind == "doc" and not d.maintainer for d in docs.values())


def test_no_docs_no_readme() -> None:
    assert fetch_policy_docs(cast(GitHubRead, StubRead({}, None)), "acme/widgets") == {}


def test_maintainer_flag_from_author_association() -> None:
    issue = {"body": "Please fix.", "author_association": "NONE"}
    comments = [
        {"body": "We do not accept AI-generated pull requests.", "author_association": "MEMBER"},
        {"body": "AI PRs are welcome!", "author_association": "CONTRIBUTOR"},
        {"body": "ok", "author_association": "OWNER"},
    ]
    docs = maintainer_docs("acme/widgets", 7, issue, comments)
    assert [d.maintainer for d in docs] == [False, True, False, True]
    assert all(d.kind == "issue" for d in docs)
    assert docs[0].path == "acme/widgets#7 issue" and docs[1].path == "acme/widgets#7 comment"
    assert maintainer_docs("acme/widgets", None, None, []) == []
