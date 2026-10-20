"""scripts/dev/demo/personal_check.py: profile values, secrets and host paths; hits never print a value."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import personal_check

PROFILE = "github_login: zz-planted-login\ncommit_name: Planted Owner Name\ncommit_email: planted.owner@example.net\n"


def _setup(tmp_path: Path, text: str) -> tuple[Path, Path]:
    (tmp_path / "config").mkdir()
    profile = tmp_path / "config" / "profile.yaml"
    profile.write_text(PROFILE, encoding="utf-8")
    target = tmp_path / "transcript.json"
    target.write_text(json.dumps({"text": text}, ensure_ascii=False) + "\n", encoding="utf-8")
    return profile, target


def test_a_planted_profile_value_prints_the_key_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    profile, target = _setup(tmp_path, "PR by zz-planted-login")
    assert personal_check.main([str(target), "--root", str(tmp_path), "--profile", str(profile)]) == 1
    out = capsys.readouterr().out
    assert "github_login" in out and "zz-planted-login" not in out


@pytest.mark.parametrize(
    "text", ["/tmp/pytest-of-x/y", "/home/someone/x", "D:\\Work\\x", "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"]
)
def test_host_paths_and_secrets_are_hits(tmp_path: Path, text: str) -> None:
    profile, target = _setup(tmp_path, text)
    assert personal_check.main([str(target), "--root", str(tmp_path), "--profile", str(profile)]) == 1


def test_a_clean_transcript_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    profile, target = _setup(tmp_path, "Test Person <test.person@example.org> · test-bot-login/widgets")
    assert personal_check.main([str(target), "--root", str(tmp_path), "--profile", str(profile)]) == 0
    assert "0 hit(s)" in capsys.readouterr().out
