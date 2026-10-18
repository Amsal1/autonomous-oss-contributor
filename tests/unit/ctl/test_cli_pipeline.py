"""``ossctl discover``, ``ossctl contribution import`` and the worker side of ``ossctl targets verify``
(FEAT-006 item 30): request bodies, the bundle upload (bytes, never a path) and the target checks."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ossbot.ctl import cli
from ossbot.runner import verify_targets
from ossbot.runner.github_read import GitHubRead
from tests.fakes.fake_github import FakeGitHub


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, Any]]:
    seen: list[tuple[str, str, Any]] = []

    def api(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30.0) -> tuple[int, Any]:
        seen.append((method, path, body))
        if path == "/v1/discover":
            return 202, {"requested_at": "2026-10-18T00:00:00Z", "repos": body["repos"] if body else []}
        return 201, {"cid": "c_0000000001", "state": "IMPORTED"}

    monkeypatch.setattr(cli, "api", api)
    return seen


def test_discover_sends_the_repos(calls: list[tuple[str, str, Any]]) -> None:
    result = CliRunner().invoke(cli.app, ["discover", "langfuse/langfuse"])
    assert result.exit_code == 0, result.output
    assert calls == [("POST", "/v1/discover", {"repos": ["langfuse/langfuse"]})]
    assert CliRunner().invoke(cli.app, ["discover"]).exit_code == 0
    assert calls[-1][2] == {"repos": []}


def test_contribution_import_uploads_bytes(calls: list[tuple[str, str, Any]], tmp_path: Path) -> None:
    bundle = tmp_path / "change.bundle"
    bundle.write_bytes(b"# v2 git bundle\n" + b"x" * 100)
    (tmp_path / "title").write_text("feat: V3 ACP adapter\nignored second line\n")
    (tmp_path / "body").write_text("Adds the V3 adapter.\n")
    result = CliRunner().invoke(cli.app, [
        "contribution", "import", "--repo", "tobenwarrior/hermes-kiro-provider", "--from-bundle", str(bundle),
        "--base", "a" * 40, "--base-ref", "main", "--title-file", str(tmp_path / "title"),
        "--body-file", str(tmp_path / "body"),
    ])  # fmt: skip
    assert result.exit_code == 0, result.output
    method, path, body = calls[0]
    assert (method, path) == ("POST", "/v1/contributions/import")
    assert base64.b64decode(body["bundle_b64"]) == bundle.read_bytes()
    assert body["bundle_sha256"] == hashlib.sha256(bundle.read_bytes()).hexdigest()
    assert body["title"] == "feat: V3 ACP adapter" and body["issue_number"] is None
    assert str(bundle) not in json.dumps(body)  # the gatekeeper never gets a path (M3)


def test_targets_verify_rows() -> None:
    gh = FakeGitHub()
    now = datetime(2026, 10, 18, tzinfo=UTC)
    gh.add_repo("langfuse/langfuse", pushed_at="2026-10-17T00:00:00Z", stargazers_count=9, default_branch="main")
    gh.add_repo("old/thing", pushed_at="2025-01-01T00:00:00Z")
    gh.add_repo("gone/archived", archived=True, pushed_at="2026-10-01T00:00:00Z")
    rows = verify_targets.verify(GitHubRead(gh.sync_client()), ["langfuse/langfuse", "old/thing", "gone/archived",
                                                                 "no/such"], 60, now)  # fmt: skip
    expected = [("langfuse/langfuse", "PASS"), ("old/thing", "WARN"), ("gone/archived", "FAIL"), ("no/such", "FAIL")]
    assert [(r["repo"], r["status"]) for r in rows] == expected
    assert rows[1]["detail"].startswith("no push for")
