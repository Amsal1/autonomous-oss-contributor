"""The project credit linked from every PR's disclosure footer (DESIGN §12.3) and from every comment ossbot posts.

``policy.text.project_url`` overrides the URL (a fork points it at its own repo). This file is an attribution
file for ``ossctl lint personal`` (like NOTICE): it carries the author's repo URL and the credit line, nothing else.
"""

from __future__ import annotations

PROJECT_NAME = "autonomous-oss-contributor"
PROJECT_URL = "https://github.com/Amsal1/autonomous-oss-contributor"

# The one-line footer of every issue or PR comment (claims, watcher replies). It matches the PR footer's wording
# (prompts/pr_disclosure.md): no tool or model named, a link to the project, the human who approved it.
COMMENT_DISCLOSURE = (
    '<sub>🤖 AI assistance disclosure: prepared with <a href="{project_url}">{project_name}</a>; '
    "reviewed and approved by @{login} before posting.</sub>"
)


def comment_disclosure(login: str, project_url: str = PROJECT_URL) -> str:
    return COMMENT_DISCLOSURE.format(project_url=project_url, project_name=PROJECT_NAME, login=login)
