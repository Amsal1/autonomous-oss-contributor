"""Doctor's GitHub rate-limit check (discovery fix 6): ``ossbot.runner.rate_limit`` runs as uid ossrunner
(``runuser``), the read token goes only into the child's environment, and the check judges the core and
search buckets: PASS, WARN below 1000 core requests or under the documented 30 searches, FAIL on a failed
or garbled answer. Quick mode runs nothing."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from ossbot.ctl import doctor

TOKEN = "github_pat_" + "E" * 30


def answer(core_remaining: int = 4210, search_limit: int = 30) -> str:
    return json.dumps({"core": {"limit": 5000, "used": 5000 - core_remaining, "remaining": core_remaining,
                                "reset": 1_790_003_600},
                       "search": {"limit": search_limit, "used": 0, "remaining": search_limit,
                                  "reset": 1_790_000_060}})  # fmt: skip


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(doctor, "_secret", lambda *a, **k: None)
    monkeypatch.setattr(doctor, "env_value",
                        lambda f, k: TOKEN if (f, k) == ("runner.env", "GITHUB_READ_TOKEN") else None)  # fmt: skip
    state: dict[str, Any] = {"stdout": answer(), "rc": 0, "calls": []}

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        state["calls"].append((argv, kw))
        if state.get("raise"):
            raise subprocess.TimeoutExpired(argv, 60)
        return subprocess.CompletedProcess(argv, state["rc"], state["stdout"], "")

    monkeypatch.setattr(doctor.subprocess, "run", run)
    return state


def test_pass_shows_core_and_search_and_runs_as_ossrunner(rig: dict[str, Any]) -> None:
    res = doctor.check_github_rate_limit(quick=False)
    assert res.status == doctor.PASS
    assert res.detail == "core 4210/5000 left (used 790, reset 15:13Z) · search 30/30 left"
    ((argv, kw),) = rig["calls"]
    assert argv[:4] == ["/usr/sbin/runuser", "-u", "ossrunner", "--"]
    assert argv[4:] == [doctor.APP_PYTHON, "-I", "-m", "ossbot.runner.rate_limit"]
    assert kw["env"]["GITHUB_READ_TOKEN"] == TOKEN and kw["cwd"] == "/" and kw["timeout"] == 60
    assert TOKEN not in " ".join(argv) and TOKEN not in res.detail and TOKEN not in res.hint


def test_low_core_warns(rig: dict[str, Any]) -> None:
    rig["stdout"] = answer(core_remaining=900)
    res = doctor.check_github_rate_limit(quick=False)
    assert res.status == doctor.WARN and "core 900/5000" in res.detail and "1000" in res.hint


def test_a_search_limit_under_30_warns(rig: dict[str, Any]) -> None:
    rig["stdout"] = answer(search_limit=10)
    res = doctor.check_github_rate_limit(quick=False)
    assert res.status == doctor.WARN and "search 10/10 left" in res.detail and "shared" in res.hint


@pytest.mark.parametrize(
    ("stdout", "rc"),
    [(json.dumps({"error": "github_unavailable"}), 1), ("not json", 0), (json.dumps({"core": {}}), 0), ("", 1)],
    ids=["error", "junk", "missing-fields", "no-output"],
)
def test_a_failed_or_garbled_answer_fails(rig: dict[str, Any], stdout: str, rc: int) -> None:
    rig["stdout"], rig["rc"] = stdout, rc
    res = doctor.check_github_rate_limit(quick=False)
    assert res.status == doctor.FAIL and TOKEN not in res.detail


def test_a_timeout_fails(rig: dict[str, Any]) -> None:
    rig["raise"] = True
    res = doctor.check_github_rate_limit(quick=False)
    assert res.status == doctor.FAIL and res.detail == "TimeoutExpired"


def test_quick_runs_no_subprocess(rig: dict[str, Any]) -> None:
    res = doctor.check_github_rate_limit(quick=True)
    assert res.status == doctor.SKIP and rig["calls"] == []


def test_a_missing_token_is_reported_without_a_run(rig: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    awaiting = doctor.Result(doctor.AWAITING, "x", "AWAITING SECRET: GITHUB_READ_TOKEN")
    monkeypatch.setattr(doctor, "_secret", lambda *a, **k: awaiting)
    assert doctor.check_github_rate_limit(quick=False) is awaiting and rig["calls"] == []
