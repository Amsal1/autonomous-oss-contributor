"""``ossctl lint personal`` strict tier: ``forbidden_logins``, the ``@domain`` entries of
``confidentiality_floor`` and the host of ``kiro.idc_start_url`` are refused in every tracked file, docs/
and the attribution files included; the other personal values keep their exemption there. Uses
tests/fixtures/profile.test.yaml (fake values only) as the profile."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ossbot.common.yamlio import safe_load
from ossbot.ctl import cli
from ossbot.ctl import lint_personal as lp

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "profile.test.yaml"
GIT = shutil.which("git") or "/usr/bin/git"
# the fake values of the fixture
WORK_LOGIN = "test-work-login"
WORK_DOMAIN = "corp.example"
IDC_HOST = "test-org.awsapps.com"
OWNER_LOGIN = "test-bot-login"


def _git(root: Path, *args: str) -> None:
    subprocess.run([GIT, "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / "config").mkdir()
    shutil.copyfile(FIXTURE, tmp_path / "config" / "profile.yaml")
    (tmp_path / ".gitignore").write_text("config/profile.yaml\n", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore")
    return tmp_path


def _track(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-f", rel)


def test_strict_values_from_the_fixture() -> None:
    profile = safe_load(FIXTURE.read_bytes())
    assert lp.strict_values(profile) == [
        ("forbidden_logins", WORK_LOGIN),
        ("confidentiality_floor", WORK_DOMAIN),
        ("kiro.idc_start_url (host)", IDC_HOST),
    ]


def test_strict_value_in_docs_is_a_hit(repo: Path) -> None:
    _track(repo, "docs/x.md", f"intro\nnever use {WORK_LOGIN.upper()} for this\n")
    assert lp.run(repo)[0] == [lp.Hit("docs/x.md", 2, "forbidden_logins")]


def test_domain_hits_inside_an_email_and_a_subdomain(repo: Path) -> None:
    _track(repo, "docs/mail.md", f"someone@{WORK_DOMAIN}\nhttps://wiki.{WORK_DOMAIN}/page\nnotcorp.example\n")
    hits = lp.run(repo)[0]
    assert [(h.line, h.key) for h in hits] == [(1, "confidentiality_floor"), (2, "confidentiality_floor")]


def test_idc_host_alone_is_a_hit(repo: Path) -> None:
    _track(repo, "docs/adr/0006.md", f"sign in at {IDC_HOST} first\n")
    _track(repo, "docs/ok.md", "https://<org>.awsapps.com/start\n")  # the placeholder is fine
    assert lp.run(repo)[0] == [lp.Hit("docs/adr/0006.md", 1, "kiro.idc_start_url (host)")]


def test_non_strict_value_in_docs_and_attribution_files_stays_exempt(repo: Path) -> None:
    _track(repo, "docs/design.md", f"author {OWNER_LOGIN}, Test Person\n")
    _track(repo, "NOTICE", f"Copyright 2026 Test Person (github.com/{OWNER_LOGIN})\n")
    _track(repo, "SECURITY.md", f"https://github.com/{OWNER_LOGIN}/x/security/advisories/new\n")
    assert lp.run(repo)[0] == []


def test_strict_value_in_an_attribution_file_is_a_hit(repo: Path) -> None:
    _track(repo, "NOTICE", f"Copyright 2026 Test Person <someone@{WORK_DOMAIN}>\n")
    assert lp.run(repo)[0] == [lp.Hit("NOTICE", 1, "confidentiality_floor")]


def test_non_strict_value_outside_docs_is_still_a_hit(repo: Path) -> None:
    _track(repo, "src/a.py", f"WHO = '{OWNER_LOGIN}'\n")
    # (the fixture's interests.summary "test" also matches "test-bot-login"; only github_login matters here)
    assert lp.Hit("src/a.py", 1, "github_login") in lp.run(repo)[0]


def test_one_hit_per_line_and_key_outside_docs(repo: Path) -> None:
    # the domain is both a personal value (@domain) and a strict one (domain): reported once
    _track(repo, "src/b.py", f"MAIL = 'x@{WORK_DOMAIN}'\n")
    assert lp.run(repo)[0] == [lp.Hit("src/b.py", 1, "confidentiality_floor")]


def test_staged_mode_checks_docs_too(repo: Path) -> None:
    _track(repo, "docs/s.md", f"{IDC_HOST}\n")
    (repo / "docs" / "s.md").write_text("clean\n", encoding="utf-8")  # worktree clean, index not
    assert lp.run(repo)[0] == []
    assert lp.run(repo, staged=True)[0] == [lp.Hit("docs/s.md", 1, "kiro.idc_start_url (host)")]


def test_cli_never_prints_a_strict_value(repo: Path) -> None:
    _track(repo, "docs/p.md", f"{WORK_LOGIN} {WORK_DOMAIN} {IDC_HOST}\n")
    res = CliRunner().invoke(cli.app, ["lint", "personal", "--root", str(repo)])
    assert res.exit_code == 1
    assert "docs/p.md:1: value of profile key forbidden_logins" in res.output
    for value in (WORK_LOGIN, WORK_DOMAIN, IDC_HOST, "test-org"):
        assert value not in res.output
