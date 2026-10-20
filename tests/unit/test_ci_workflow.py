""".github/workflows/ci.yml (AC 24, DES-V-27): read-only permissions, every action pinned by a 40-hex SHA
that matches versions.lock.yaml, no secrets, no pull_request_target, and every required check present."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ossbot.common.yamlio import safe_load

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"
LOCK = ROOT / "config" / "versions.lock.yaml"
USES_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")


def _ci() -> dict[str, Any]:
    data = safe_load(CI.read_bytes())
    assert isinstance(data, dict)
    return data


def _lock() -> dict[str, Any]:
    data = safe_load(LOCK.read_bytes())
    assert isinstance(data, dict)
    return data


def _steps(ci: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for job in ci["jobs"].values() for step in job["steps"]]


def _runs(ci: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in _steps(ci))


def test_permissions_are_contents_read_only() -> None:
    ci = _ci()
    assert ci["permissions"] == {"contents": "read"}
    for name, job in ci["jobs"].items():
        assert job.get("permissions", {"contents": "read"}) == {"contents": "read"}, name


def test_triggers_are_push_and_pull_request_only() -> None:
    assert set(_ci()["on"]) == {"push", "pull_request"}
    assert "pull_request_target" not in CI.read_text(encoding="utf-8")


def test_no_secrets_are_referenced() -> None:
    text = CI.read_text(encoding="utf-8")
    assert "secrets." not in text and "GITHUB_TOKEN" not in text


def test_every_action_is_pinned_by_full_sha() -> None:
    uses = [step["uses"] for step in _steps(_ci()) if "uses" in step]
    assert uses
    for ref in uses:
        assert USES_RE.match(ref), f"not pinned by a 40-hex commit SHA: {ref}"


def test_pins_match_versions_lock() -> None:
    lock, ci = _lock(), _ci()
    pins = lock["github_actions"]
    uses = {step["uses"] for step in _steps(ci) if "uses" in step}
    assert uses == {f"actions/checkout@{pins['checkout']}", f"astral-sh/setup-uv@{pins['setup_uv']}"}
    env = ci["env"]
    assert env["UV_VERSION"] == lock["uv"]["version"] and env["UV_CHECKSUM"] == lock["uv"]["sha256"]
    assert env["GITLEAKS_URL"] == lock["gitleaks"]["url"] and env["GITLEAKS_SHA256"] == lock["gitleaks"]["sha256"]
    assert "sha256sum -c" in _runs(ci)


def test_checkouts_do_not_persist_credentials() -> None:
    for step in _steps(_ci()):
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            assert step["with"]["persist-credentials"] is False


def test_required_checks_are_present() -> None:
    runs = _runs(_ci())
    for needle in (
        "ruff check src tests",
        "ruff format --check src tests",
        "mypy",
        "lint-imports",
        "shellcheck",
        'pytest -m "unit or telegram or acp or e2e"',
        "gitleaks git --redact --no-banner --config .gitleaks.toml .",
        "ossctl docs commands --check",
        "tests/unit/common/test_profile_example.py",
        "ossctl lint personal",
        "scripts/dev/psscriptanalyzer.ps1",
    ):
        assert needle in runs, needle


def test_gitleaks_job_scans_full_history() -> None:
    steps = _ci()["jobs"]["secrets"]["steps"]
    assert steps[0]["with"]["fetch-depth"] == 0
