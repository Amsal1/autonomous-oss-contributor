"""Doctor's read-only GitHub MCP check (GHMCP-3/4): runs the wrapper as uid hermes via squid 3141 and
judges only its JSON metadata; the token goes into the child's environment, never into argv or output."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ossbot.ctl import doctor

TOKEN = "github_pat_" + "D" * 30


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    wrapper = tmp_path / "ossbot-github-mcp"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "GITHUB_MCP_WRAPPER", wrapper)
    monkeypatch.setattr(doctor, "_secret", lambda *a, **k: None)
    monkeypatch.setattr(
        doctor, "env_value", lambda f, k: TOKEN if (f, k) == ("hermes.env", "GITHUB_READ_TOKEN") else None
    )
    monkeypatch.setattr(doctor, "_top_profile_value", lambda key: "Amsal1" if key == "github_login" else None)
    state: dict[str, Any] = {"stdout": "", "rc": 0, "calls": []}

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        state["calls"].append((argv, kw["env"]))
        return subprocess.CompletedProcess(argv, state["rc"], state["stdout"], "")

    monkeypatch.setattr(doctor.subprocess, "run", run)
    return state


def test_live_check_passes_on_the_owner_login(rig: dict[str, Any]) -> None:
    rig["stdout"] = json.dumps({"ok": True, "login": "amsal1", "entries": 12})
    res = doctor.check_github_mcp(quick=False)
    assert res.status == doctor.PASS and "get_me amsal1" in res.detail
    ((argv, env),) = rig["calls"]
    assert argv[:4] == ["/usr/sbin/runuser", "-u", "hermes", "--"] and argv[-2:] == [
        "--doctor",
        "github/github-mcp-server",
    ]
    assert TOKEN not in " ".join(argv) and env["GITHUB_READ_TOKEN"] == TOKEN
    assert env["HTTPS_PROXY"] == "http://10.212.0.2:3141"
    assert TOKEN not in res.detail


@pytest.mark.parametrize(
    ("stdout", "rc"),
    [
        (json.dumps({"ok": True, "login": "someone-else", "entries": 3}), 0),
        (json.dumps({"ok": False, "refused": ["create_issue"]}), 3),
        ("", 3),
        ("not json", 1),
    ],
)
def test_wrong_login_write_tool_or_no_answer_fails(rig: dict[str, Any], stdout: str, rc: int) -> None:
    rig["stdout"], rig["rc"] = stdout, rc
    assert doctor.check_github_mcp(quick=False).status == doctor.FAIL


def test_quick_runs_only_the_startup_check(rig: dict[str, Any]) -> None:
    rig["stdout"] = json.dumps({"ok": True, "tools": 26, "refused": []})
    res = doctor.check_github_mcp(quick=True)
    assert res.status == doctor.PASS and res.detail == "26 read-only tools"
    assert rig["calls"][0][0][-1] == "--check"


def test_not_installed_is_a_warning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "GITHUB_MCP_WRAPPER", tmp_path / "absent")
    assert doctor.check_github_mcp(quick=True).status == doctor.WARN
