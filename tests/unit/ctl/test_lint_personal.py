"""``ossctl lint personal`` (AC 23): profile values in tracked files outside docs/ are reported by key,
never by value; docs/ is exempt; a tracked profile is refused; no profile means nothing to check."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ossbot.ctl import cli
from ossbot.ctl import lint_personal as lp

PROFILE = """version: 1
github_login: octo-owner
commit_name: "Jane Q Owner"
commit_email: jane.owner@mail.test
forbidden_logins: [jane-at-work]
confidentiality_floor: ["@corp.test"]
telegram: {user_id: 424242, timezone: UTC}
kiro: {idc_start_url: "https://corp-x.awsapps.com/start", idc_region: us-east-1}
backup: {age_recipients: ["age1qqqqqqqqqqqqqq"]}
review_mirror: {repo: null}
"""


GIT = shutil.which("git") or "/usr/bin/git"


def _git(root: Path, *args: str) -> None:
    subprocess.run([GIT, "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "profile.yaml").write_text(PROFILE, encoding="utf-8")
    (tmp_path / ".gitignore").write_text("config/profile.yaml\n", encoding="utf-8")
    (tmp_path / "clean.py").write_text("print('hello')\n", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore", "clean.py")
    return tmp_path


def _track(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-f", rel)


def test_clean_repo_passes(repo: Path) -> None:
    hits, summary = lp.run(repo)
    assert hits == []
    assert "0 hit(s)" in summary


def test_values_found_by_key_with_line(repo: Path) -> None:
    _track(repo, "src/a.py", "x = 1\nOWNER = 'Octo-Owner'\n")  # case-insensitive
    _track(repo, "tests/b.txt", "mail jane.owner@mail.test and 424242\n")
    hits, _ = lp.run(repo)
    found = {(h.path, h.line, h.key) for h in hits}
    assert ("src/a.py", 2, "github_login") in found
    assert ("tests/b.txt", 1, "commit_email") in found
    assert ("tests/b.txt", 1, "telegram.user_id") in found


def test_staged_new_file_is_checked(repo: Path) -> None:
    _track(repo, "new.md", "see @corp.test\n")  # staged only, never committed
    assert [h.key for h in lp.run(repo)[0]] == ["confidentiality_floor"]


def test_docs_are_exempt_and_word_boundaries_hold(repo: Path) -> None:
    _track(repo, "docs/design.md", "octo-owner everywhere\n")
    _track(repo, "src/c.py", "malocto-owner2 = 1  # glued letters are not the login\n")
    assert lp.run(repo)[0] == []


def test_untracked_files_are_ignored(repo: Path) -> None:
    (repo / "scratch.txt").write_text("octo-owner\n", encoding="utf-8")
    assert lp.run(repo)[0] == []


def test_tracked_profile_is_refused(repo: Path) -> None:
    _git(repo, "add", "-f", "config/profile.yaml")
    with pytest.raises(lp.LintError, match="tracked"):
        lp.run(repo)


def test_no_profile_passes(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    hits, summary = lp.run(tmp_path)
    assert hits == [] and "nothing to compare" in summary


def test_short_zero_and_empty_values_are_skipped() -> None:
    values = lp.personal_values({"github_login": "ab", "telegram": {"user_id": 0},
                                 "review_mirror": {"repo": None}, "commit_name": "Jane Owner"})  # fmt: skip
    assert values == [("commit_name", "Jane Owner")]


def test_cli_prints_keys_never_values(repo: Path) -> None:
    _track(repo, "src/a.py", "who = 'Jane Q Owner'\n")
    res = CliRunner().invoke(cli.app, ["lint", "personal", "--root", str(repo)])
    assert res.exit_code == 1
    assert "src/a.py:1: value of profile key commit_name" in res.output
    assert "Jane Q Owner" not in res.output
