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


def test_targets_facts_rows() -> None:
    """``ossctl targets facts`` (post-v1): AI policy and CI on PRs per repo, GETs only, no gatekeeper call."""
    gh = FakeGitHub()
    for repo in ("ban/repo", "silent/repo", "ci/repo"):
        gh.add_repo(repo)
    gh.files[("ban/repo", "CONTRIBUTING.md")] = "Hi.\nWe do not accept AI-generated pull requests.\n"
    gh.add_pull("ci/repo", state="closed", merged_at="2026-10-01T00:00:00Z", head={"sha": "s1", "label": "x:s1"})
    gh.check_runs[("ci/repo", "s1")] = [{"name": "pytest", "status": "completed", "conclusion": "success"}]
    rows = verify_targets.facts(GitHubRead(gh.sync_client()), ["ban/repo", "silent/repo", "ci/repo", "no/such"])
    by = {r["repo"]: r for r in rows}
    assert by["ban/repo"]["ai_policy"] == "banned" and by["ban/repo"]["ai_source"] == "CONTRIBUTING.md"
    assert by["ban/repo"]["ai_line"] == "We do not accept AI-generated pull requests."
    assert by["silent/repo"]["ai_display"] == "not stated" and by["silent/repo"]["ci_on_pr"] == "unknown"
    assert by["ci/repo"]["ci_on_pr"] == "yes" and by["ci/repo"]["ci_display"] == "✅ tests (1/1 merged PRs)"
    assert by["no/such"]["status"] == "FAIL" and by["no/such"]["detail"].startswith("not found")
    assert gh.writes == [] and {r.method for r in gh.requests} == {"GET"}
    assert not any(r.path.startswith("/v1/") for r in gh.requests)
    assert len(verify_targets.facts(GitHubRead(gh.sync_client()), [f"o/r{i}" for i in range(9)])) == 5


def test_targets_verify_and_facts_show_an_inactive_default_branch() -> None:
    """Owner addition (2026-10-21): a seed target whose default branch is 7 months old is a WARN in
    ``targets verify`` and flagged in ``targets facts`` (discovery skips it; it stays in targets.yaml)."""
    gh = FakeGitHub()
    now = datetime(2026, 10, 18, tzinfo=UTC)
    gh.add_repo("old/branch", pushed_at="2026-10-17T00:00:00Z", default_branch="main")
    gh.commit_dates["old/branch"] = "2026-03-01T00:00:00Z"
    gh.add_repo("new/branch", pushed_at="2026-10-17T00:00:00Z", default_branch="trunk")
    gh.commit_dates["new/branch"] = "2026-10-16T00:00:00Z"
    rows = verify_targets.verify(GitHubRead(gh.sync_client()), ["old/branch", "new/branch"], 60, now)
    assert [(r["repo"], r["status"]) for r in rows] == [("old/branch", "WARN"), ("new/branch", "PASS")]
    assert rows[0]["detail"].startswith("inactive_default_branch: no commit on main for 231 days")
    by = {r["repo"]: r for r in verify_targets.facts(GitHubRead(gh.sync_client()), ["old/branch", "new/branch"],
                                                     now)}  # fmt: skip
    assert (by["old/branch"]["inactive"], by["old/branch"]["branch_days"]) == (True, 231)
    assert (by["new/branch"]["inactive"], by["new/branch"]["branch_days"]) == (False, 2)
    assert "⚠️ inactive_default_branch" in cli._branch_line(by["old/branch"])
    assert cli._branch_line(by["new/branch"]) == " · last commit on trunk: 2 d ago"


# ------------------------------------------------------------------ post-v1 open discovery (§10)
def search_fake() -> FakeGitHub:
    gh = FakeGitHub()
    base = {"stargazers_count": 20000, "pushed_at": "2026-10-16T00:00:00Z", "language": "Go",
            "license": {"spdx_id": "Apache-2.0"}, "topics": ["gitops"], "has_issues": True,
            "visibility": "public", "default_branch": "main"}  # fmt: skip
    gh.add_repo("acme/top", **base)
    gh.add_repo("acme/helm", **{**base, "stargazers_count": 3000})
    gh.add_repo("acme/forked", **{**base, "fork": True})
    gh.add_repo("acme/nodocs", **base)
    gh.add_repo("langfuse/langfuse", **base)  # a config target: excluded
    for repo in ("acme/top", "acme/helm", "acme/forked", "langfuse/langfuse"):
        gh.files[(repo, "CONTRIBUTING.md")] = "Be kind."
    gh.files[("acme/helm", "Chart.yaml")] = "dependencies:\n  - repository: https://charts.example.io\n"
    gh.add_pull("acme/helm", state="closed", merged_at="2026-10-01T00:00:00Z", head={"sha": "h1", "label": "x:h1"})
    gh.check_runs[("acme/helm", "h1")] = [{"name": "pytest", "status": "completed", "conclusion": "success"}]
    return gh


SEARCH_REQ: dict[str, Any] = {
    "mode": "search", "topics": ["gitops"], "min_stars": 1000, "pushed_within_days": 60,
    "languages_preferred": ["Go"], "language_weight": 6.0, "ci_weight": 6.0, "top": 10,
    "exclude": ["langfuse/langfuse"], "deny_patterns": [], "own_logins": ["test-bot-login"],
    "allowed_hosts": ["github.com", "proxy.golang.org"], "max_default_branch_age_days": 180,
}  # fmt: skip


def test_targets_search_dry_run_is_get_only_and_ranked() -> None:
    gh = search_fake()
    doc = verify_targets.search(GitHubRead(gh.sync_client(), sleep=lambda s: None), SEARCH_REQ,
                                datetime(2026, 10, 18, tzinfo=UTC))  # fmt: skip
    assert doc["ok"] is True
    assert [r["repo"] for r in doc["rows"]] == ["acme/helm", "acme/top"]  # CI on PRs moves helm first
    helm = doc["rows"][0]
    assert helm["ci_on_pr"] == "yes" and helm["would_skip_if_strict"] == "egress_extra_needed:charts.example.io"
    assert doc["rows"][1]["would_skip_if_strict"] is None
    reasons = {r["repo"]: r["reason"] for r in doc["filtered"]}
    assert reasons == {"acme/forked": "fork", "langfuse/langfuse": "excluded", "acme/nodocs": "no_contributing"}
    assert gh.writes == [] and {r.method for r in gh.requests} == {"GET"}
    assert not any(r.path.startswith("/v1/") for r in gh.requests)
    q = next(r.params for r in gh.requests if r.path == "/search/repositories")
    assert q == {"q": "topic:gitops stars:>=1000 pushed:>=2026-08-19 archived:false mirror:false is:public",
                 "sort": "stars", "order": "desc", "per_page": "50"}  # fmt: skip


def test_targets_search_rate_limit_exits_1(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import io

    gh = search_fake()
    gh.answers["GET /search/repositories"] = [(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1793000000"},
                                               {"message": "API rate limit exceeded"})]  # fmt: skip
    monkeypatch.setattr(verify_targets, "GitHubRead", lambda: GitHubRead(gh.sync_client(), sleep=lambda s: None))
    monkeypatch.setattr(verify_targets.sys, "stdin", io.StringIO(json.dumps(SEARCH_REQ)))
    assert verify_targets.main() == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["error"] == "rate_limited" and doc["until"] > 0 and gh.writes == []


def test_targets_search_builds_the_request_and_prints(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from tests.unit.common.test_config import bundle_files

    monkeypatch.setattr(cli, "_require_root", lambda what: None)
    monkeypatch.setattr(cli, "read_current", lambda root: ("h", bundle_files()))
    gets: list[str] = []

    def api(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30.0) -> tuple[int, Any]:
        assert method == "GET"
        gets.append(path)
        if path == "/v1/discovery-prefs":
            return 200, {"effective": {"open": {"enabled": True, "topics": ["helm", "sre"],
                                                "languages_preferred": ["Rust"]}}}  # fmt: skip
        return 200, [{"repo": "never/one"}] if "never" in path else [{"repo": "kept/two"}]

    monkeypatch.setattr(cli, "api", api)
    ran: list[tuple[list[str], str]] = []
    out = {"ok": True, "rows": [{"repo": "acme/top", "stars": 20000, "language": "Go", "ci_on_pr": "yes",
                                 "pushed_at": "2026-10-16T00:00:00Z", "license": "Apache-2.0", "topic": "helm",
                                 "score": 27.5, "would_skip_if_strict": None}],
           "filtered": [{"repo": "acme/forked", "reason": "fork", "topic": "helm"}]}  # fmt: skip

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        ran.append((argv, kw.get("input") or ""))
        return subprocess.CompletedProcess(argv, 0, json.dumps(out) + "\n" if kw.get("input") else "", "")

    monkeypatch.setattr(cli.subprocess, "run", run)
    result = CliRunner().invoke(cli.app, ["targets", "search", "--top", "5", "--all"])
    assert result.exit_code == 0, result.output
    assert ran[0][0] == [cli.FIREWALL_CHECK, "--check"] and ran[1][0] == cli.targets_verify_argv()
    req = json.loads(ran[1][1])
    assert (req["mode"], req["top"], req["topics"], req["languages_preferred"]) == ("search", 5, ["helm", "sre"],
                                                                                   ["Rust"])  # fmt: skip
    assert {"langfuse/langfuse", "never/one", "kept/two"} <= set(req["exclude"])
    assert "test-bot-login" in req["own_logins"] and "pypi.org" in req["allowed_hosts"]
    assert req["max_default_branch_age_days"] == 180
    assert gets == ["/v1/discovery-prefs", "/v1/discovered-targets?status=never&limit=500",
                    "/v1/discovered-targets?status=kept&limit=500"]  # fmt: skip
    assert "acme/top" in result.output and "filtered acme/forked (helm): fork" in result.output
    topic = CliRunner().invoke(cli.app, ["targets", "search", "--topic", "gitops"])
    assert topic.exit_code == 0 and json.loads(ran[-1][1])["topics"] == ["gitops"]


@pytest.mark.parametrize(
    ("argv", "decision"),
    [(["add", "acme/tool"], "keep"), (["block", "acme/tool"], "never"), (["unblock", "acme/tool"], "unblock"),
     (["unblock", "acme/tool", "--unkeep"], "unkeep"), (["strict", "acme/tool", "--off"], "unstrict")],
)  # fmt: skip
def test_targets_decisions_need_a_tty_and_send_the_decision(monkeypatch: pytest.MonkeyPatch, argv: list[str],
                                                            decision: str) -> None:  # fmt: skip
    sent: list[Any] = []

    def api(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30.0) -> tuple[int, Any]:
        sent.append((method, path, body))
        return 200, {"view": {"repo": "acme/tool", "status": "kept", "egress_strict": False}}

    monkeypatch.setattr(cli, "api", api)
    refused = CliRunner().invoke(cli.app, ["targets", *argv])  # CliRunner's stdin is not a TTY
    assert refused.exit_code == 2 and "interactive TTY confirm" in refused.output and sent == []
    words: list[str] = []
    monkeypatch.setattr(cli, "_confirm", words.append)
    ok = CliRunner().invoke(cli.app, ["targets", *argv])
    assert ok.exit_code == 0, ok.output
    assert sent == [("POST", "/v1/discovered-targets/decision", {"repo": "acme/tool", "decision": decision})]
    assert words == [{"keep": "KEEP", "never": "BLOCK", "unblock": "UNBLOCK", "unkeep": "UNBLOCK",
                      "unstrict": "WIDEN"}[decision]]  # fmt: skip


def test_targets_strict_on_needs_no_confirm_and_refusals_are_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    view = {"view": {"repo": "acme/tool", "status": "candidate", "egress_strict": True}}
    answers = [
        (200, view),
        (409, {"error": "is_target"}),
        (409, {"error": "already_target"}),
        (400, {"error": "denied"}),
    ]
    monkeypatch.setattr(cli, "api", lambda method, path, body=None, timeout=30.0: answers.pop(0))
    monkeypatch.setattr(cli, "_confirm", lambda word: None)
    ok = CliRunner().invoke(cli.app, ["targets", "strict", "acme/tool"])
    assert ok.exit_code == 0 and "strict egress: acme/tool is now candidate (strict egress)" in ok.output
    target = CliRunner().invoke(cli.app, ["targets", "strict", "langfuse/langfuse"])
    assert target.exit_code == 1 and "set egress: strict in config/targets.yaml" in target.output
    assert "already in config/targets.yaml" in CliRunner().invoke(cli.app, ["targets", "add", "langfuse/x"]).output
    assert "denylisted or forbidden owner" in CliRunner().invoke(cli.app, ["targets", "add", "aosp-mirror/x"]).output
    bad = CliRunner().invoke(cli.app, ["targets", "add", "not a repo"])
    assert bad.exit_code == 1 and "owner/name" in bad.output


def test_targets_discovered_table(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"repo": "acme/top", "status": "skipped", "source": "search", "topic": "gitops", "stars": 20000,
             "language": "Go", "ci_on_pr": "yes", "egress_strict": True, "skip_reason": "no_contributing",
             "checked_at": None, "cooldown_until": "2026-11-01T00:00:00Z"}]  # fmt: skip
    paths: list[str] = []

    def api(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30.0) -> tuple[int, Any]:
        paths.append(path)
        return 200, rows

    monkeypatch.setattr(cli, "api", api)
    result = CliRunner().invoke(cli.app, ["targets", "discovered", "--status", "skipped"])
    assert result.exit_code == 0, result.output
    assert paths == ["/v1/discovered-targets?status=skipped"]
    line = next(ln for ln in result.output.splitlines() if ln.startswith("acme/top"))
    assert "skipped" in line and "yes" in line and "no_contributing / - / 2026-11-01T00:00:00Z" in line
    assert CliRunner().invoke(cli.app, ["targets", "discovered", "--status", "bogus"]).exit_code == 1


def test_targets_facts_refuses_more_than_five_repos(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []
    monkeypatch.setattr(cli, "_require_root", lambda what: None)
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **kw: ran.append(argv))
    result = CliRunner().invoke(cli.app, ["targets", "facts", *[f"o/r{i}" for i in range(6)]])
    assert result.exit_code != 0 and "1 to 5 repos" in result.output and ran == []
    bad = CliRunner().invoke(cli.app, ["targets", "facts", "not a repo"])
    assert bad.exit_code != 0 and "owner/name" in bad.output and ran == []


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
