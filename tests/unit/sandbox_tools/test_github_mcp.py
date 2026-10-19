"""ossbot-github-mcp (GHMCP-1..4): fixed read-only flags, the token handling and the fail-closed
startup check against a fake MCP listing (``tests/fakes/fake_github_mcp.py``)."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from ossbot.sandbox_tools import github_mcp as gm

FAKE = Path(__file__).resolve().parents[2] / "fakes" / "fake_github_mcp.py"
TOKEN = "github_pat_" + "F" * 30
READ = {"annotations": {"readOnlyHint": True}}
LISTING = [
    {"name": "get_me", **READ},
    {"name": "get_file_contents", **READ},
    {"name": "search_code", **READ},
    {"name": "list_workflow_runs", **READ},
    {"name": "get_workflow_run", **READ},
    {"name": "issue_read", **READ},
    {"name": "pull_request_read", **READ},
    {"name": "list_starred_repositories", **READ},
]


def _server(tmp_path: Path, pages: list[list[dict[str, Any]]], **scenario: Any) -> tuple[str, Path]:
    log = tmp_path / "server.log"
    scen = tmp_path / "scenario.json"
    scen.write_text(json.dumps({"pages": pages, "log": str(log), **scenario}), encoding="utf-8")
    launcher = tmp_path / "github-mcp-server"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "{scen}" "$@"\n', encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    return str(launcher), log


def _env(tmp_path: Path) -> dict[str, str]:
    return gm.server_env({"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)}, TOKEN)


def test_fixed_read_only_flags() -> None:
    assert gm.server_argv("/x") == [
        "/x", "stdio", "--read-only", "--toolsets", "repos,issues,pull_requests,actions", "--tools", "get_me",
    ]  # fmt: skip


def test_read_tools_pass_and_writes_are_refused() -> None:
    assert gm.refused_tools(LISTING) == []
    bad = [
        {"name": "create_pull_request", **READ},  # a write verb even if it claims read-only
        {"name": "merge_pull_request", "annotations": {"readOnlyHint": False}},
        {"name": "list_branches"},  # no annotations: not proven read-only
        {"name": "Weird Name", **READ},
        "not a tool",
    ]
    assert gm.refused_tools(bad) == ["create_pull_request", "merge_pull_request", "list_branches", "<malformed>",
                                     "<malformed>"]  # fmt: skip


def test_check_reads_every_page(tmp_path: Path) -> None:
    server, log = _server(tmp_path, [LISTING[:3], LISTING[3:]])
    verdict = gm.check(gm.server_argv(server), _env(tmp_path))
    assert verdict == {"ok": True, "tools": len(LISTING), "refused": []}
    (line,) = log.read_text(encoding="utf-8").splitlines()
    seen = json.loads(line)
    assert seen["argv"][:2] == ["stdio", "--read-only"] and seen["token"] is True
    assert "GITHUB_READ_TOKEN" not in seen["env"]


def test_write_tool_listed_fails_closed(tmp_path: Path) -> None:
    server, _ = _server(tmp_path, [[*LISTING, {"name": "push_files", "annotations": {"readOnlyHint": False}}]])
    assert gm.check(gm.server_argv(server), _env(tmp_path)) == {"ok": False, "tools": 9, "refused": ["push_files"]}


def test_empty_listing_is_not_ok(tmp_path: Path) -> None:
    server, _ = _server(tmp_path, [[]])
    assert gm.check(gm.server_argv(server), _env(tmp_path))["ok"] is False


def test_token_from_env_or_home_file(tmp_path: Path) -> None:
    assert gm.read_token({"GITHUB_READ_TOKEN": TOKEN}) == TOKEN
    assert gm.read_token({"GITHUB_READ_TOKEN": "not-a-token"}) is None
    (tmp_path / ".ossbot").mkdir()
    (tmp_path / ".ossbot" / "github-read-token").write_text(TOKEN + "\n", encoding="utf-8")
    assert gm.read_token({"HOME": str(tmp_path)}) == TOKEN
    assert gm.read_token({"HOME": str(tmp_path / "nowhere")}) is None


def test_server_env_passes_only_named_variables() -> None:
    parent = {
        "PATH": "/bin",
        "HTTPS_PROXY": "http://p:1",
        "KIRO_API_KEY": "ksk_x",
        "GITHUB_READ_TOKEN": TOKEN,
        "S": "x",
    }
    env = gm.server_env(parent, TOKEN)
    assert env == {"PATH": "/bin", "HTTPS_PROXY": "http://p:1", "GITHUB_PERSONAL_ACCESS_TOKEN": TOKEN}


def test_main_without_token_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GITHUB_READ_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert gm.main([]) == 2


def test_main_execs_only_after_a_clean_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITHUB_READ_TOKEN", TOKEN)
    execs: list[tuple[str, list[str], dict[str, str]]] = []

    def fake_exec(path: str, argv: list[str], env: dict[str, str]) -> None:
        execs.append((path, argv, env))

    monkeypatch.setattr(gm.os, "execve", fake_exec)
    server, _ = _server(tmp_path, [LISTING])
    assert gm.main([], server_bin=server) == 0
    ((path, argv, env),) = execs
    assert path == server and argv == gm.server_argv(server) and env["GITHUB_PERSONAL_ACCESS_TOKEN"] == TOKEN
    assert "GITHUB_READ_TOKEN" not in env


def test_main_refuses_to_start_a_server_with_a_write_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_READ_TOKEN", TOKEN)
    monkeypatch.setattr(gm.os, "execve", lambda *a: pytest.fail("must not exec"))
    server, _ = _server(tmp_path, [[*LISTING, {"name": "create_issue", **READ}]])
    assert gm.main([], server_bin=server) == 3
    err = capsys.readouterr().err
    assert "create_issue" in err and TOKEN not in err


def test_check_and_doctor_modes_print_json_without_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_READ_TOKEN", TOKEN)
    server, _ = _server(tmp_path, [LISTING], login="Octo", entries=4)
    assert gm.main(["--check"], server_bin=server) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "tools": len(LISTING), "refused": []}
    assert gm.main(["--doctor", "octo/hello"], server_bin=server) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {"ok": True, "login": "Octo", "entries": 4} and TOKEN not in out
    assert gm.main(["--doctor", "not a repo"], server_bin=server) == 2
