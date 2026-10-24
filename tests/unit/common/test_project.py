"""The comment credit line (common/project.py): the same wording as the PR footer, no tool or model named."""

from __future__ import annotations

import pytest

from ossbot.common.project import PROJECT_NAME, PROJECT_URL, comment_disclosure
from ossbot.gatekeeper import action_requests
from ossbot.runner.stages import fetch

pytestmark = pytest.mark.unit


def test_comment_disclosure_links_the_project_and_names_the_reviewer() -> None:
    line = comment_disclosure("octo")
    assert line == (
        f'<sub>🤖 AI assistance disclosure: prepared with <a href="{PROJECT_URL}">{PROJECT_NAME}</a>; '
        "reviewed and approved by @octo before posting.</sub>"
    )
    assert "Kiro" not in line and "claude" not in line.lower()


def test_comment_disclosure_uses_a_forks_project_url() -> None:
    assert '<a href="https://example.org/fork">' in comment_disclosure("octo", "https://example.org/fork")


def test_claim_body_is_the_runner_text_plus_the_credit() -> None:
    assert action_requests.CLAIM_TEMPLATE == fetch.CLAIM_BODY
    body = action_requests.claim_body("octo", "https://example.org/fork")
    assert body == f"{fetch.CLAIM_BODY}\n\n{comment_disclosure('octo', 'https://example.org/fork')}"
    assert "drafted with AI assistance" not in body
