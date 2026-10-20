"""Repository metadata (license and community files): the issue forms parse and carry GitHub's
required keys, the license files exist and agree with pyproject, and LICENSE starts with the Apache-2.0
text whose sha256 is recorded in config/versions.lock.yaml."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

import pytest

from ossbot.common.yamlio import safe_load

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / ".github" / "ISSUE_TEMPLATE"
FORMS = sorted(p for p in TEMPLATES.glob("*.yml") if p.name != "config.yml")


def test_expected_forms_exist() -> None:
    assert {p.name for p in FORMS} == {"bug.yml", "feature.yml", "model-backend.yml"}


@pytest.mark.parametrize("path", FORMS, ids=lambda p: p.name)
def test_issue_form_has_required_keys(path: Path) -> None:
    form = safe_load(path.read_bytes())
    assert isinstance(form, dict)
    assert {"name", "description", "body"} <= form.keys()
    assert isinstance(form["body"], list) and form["body"]
    ids = [item["id"] for item in form["body"] if item.get("type") != "markdown"]
    assert len(ids) == len(set(ids)), "issue form ids must be unique"
    for item in form["body"]:
        assert item["type"] in {"markdown", "textarea", "input", "dropdown", "checkboxes"}
        assert "attributes" in item


def test_issue_config_points_to_private_advisories() -> None:
    cfg = safe_load((TEMPLATES / "config.yml").read_bytes())
    assert cfg["blank_issues_enabled"] is False
    assert any(link["url"].endswith("/security/advisories/new") for link in cfg["contact_links"])


def test_pr_template_has_ai_disclosure_and_invariants() -> None:
    text = (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    for needle in ("AI assistance", "I1-I13", "No new GitHub write path", "No exposure", "git commit -s"):
        assert needle in text


def test_license_matches_the_recorded_apache_text() -> None:
    lock = safe_load((ROOT / "config" / "versions.lock.yaml").read_bytes())["apache_license_2_0"]
    data = (ROOT / "LICENSE").read_bytes()
    assert hashlib.sha256(data).hexdigest() == lock["license_file_sha256"]
    marker = data.index(b"\n" + b"-" * 78 + b"\nThird-party components")
    assert hashlib.sha256(data[:marker]).hexdigest() == lock["sha256"]


def test_pyproject_license_and_notice() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == ["LICENSE", "NOTICE"]
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8").splitlines()
    assert notice[0] == "ossbot" and notice[1].startswith("Copyright 2026 ")
    assert notice[2].startswith("https://github.com/")
    for name in ("THIRD_PARTY_NOTICES.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md",
                 "vendor/hermes-kiro-provider/LICENSE"):  # fmt: skip
        assert (ROOT / name).is_file()
