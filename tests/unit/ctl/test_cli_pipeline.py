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


def test_targets_verify_unit_has_the_runner_hardening() -> None:
    """F006-R2-9: the transient ossrunner unit carries every hardening line of ossbot-runner.service."""
    unit = (Path(__file__).resolve().parents[3] / "systemd" / "ossbot-runner.service").read_text(encoding="utf-8")
    keys = ("UMask", "NoNewPrivileges", "ProtectSystem", "ProtectHome", "PrivateTmp", "CapabilityBoundingSet",
            "RestrictSUIDSGID", "LockPersonality", "SystemCallArchitectures", "RestrictAddressFamilies",
            "SystemCallFilter", "SystemCallErrorNumber", "EnvironmentFile")  # fmt: skip
    wanted = [ln for ln in unit.splitlines() if ln.split("=", 1)[0] in keys]
    assert len(wanted) == len(keys)
    argv = cli.targets_verify_argv()
    props = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-p"]
    assert sorted(props) == sorted(wanted)
    assert "--uid=ossrunner" in argv and argv[-2:] == ["-m", "ossbot.runner.verify_targets"]


def test_targets_verify_refuses_when_the_firewall_check_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess
    from types import SimpleNamespace

    ran: list[list[str]] = []

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "egress rules missing")

    monkeypatch.setattr(cli, "_require_root", lambda what: None)
    monkeypatch.setattr(cli, "read_current", lambda root: ("h", {}))
    monkeypatch.setattr(cli, "parse_bundle", lambda files: SimpleNamespace(targets=None))
    monkeypatch.setattr("ossbot.common.config.all_targets", lambda targets: [])
    monkeypatch.setattr(cli.subprocess, "run", run)
    result = CliRunner().invoke(cli.app, ["targets", "verify"])
    assert result.exit_code != 0 and "firewall check failed" in result.output
    assert ran == [[cli.FIREWALL_CHECK, "--check"]]  # the token is never used while the check fails


RERUN_CASES = [({"symlink": True, "settings": True}, "PASS"), ({"symlink": True, "settings": False}, "FAIL"),
               ({}, "FAIL")]  # fmt: skip


@pytest.mark.parametrize(("results", "status"), RERUN_CASES)
def test_doctor_probe_rerun_reads_the_runner_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, results: dict[str, bool], status: str
) -> None:
    """``POST /v1/probes/run`` answers ``{"results": {probe: bool}}`` (ProbeRunner.run_all)."""
    import httpx

    from ossbot.common.paths import Paths
    from ossbot.ctl import doctor
    from tests.conftest import make_repo

    paths = Paths.under(tmp_path, repo=make_repo(tmp_path))
    monkeypatch.setattr(doctor.Paths, "default", staticmethod(lambda: paths))
    sent: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"results": results})

    monkeypatch.setattr(doctor, "uds_client", lambda sock, timeout: httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://sandboxd"))  # fmt: skip
    from ossbot.common.config import parse_models

    test_model = parse_models((paths.config_dir / "models.yaml").read_bytes()).roles.test.model
    got = doctor._run_probes([{"model_id": m, "forced": False} for m in ("claude-opus-5.5", test_model)])
    assert got.status == status
    assert sent[0]["models"] == {"planner": test_model, "coder": test_model}  # roles.test only
