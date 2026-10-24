"""``ossctl egress status|mode`` with the fourth mode ``sandbox_open`` (open-discovery design §12.2) and
``egress holds|release|jobs`` (§13.2-§13.4, review-3 M3)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ossbot.common import public_floor
from ossbot.common.control_flags import EGRESS_MODES
from ossbot.ctl import cli


def _api(monkeypatch: pytest.MonkeyPatch, answer: tuple[int, Any]) -> list[tuple[str, str, Any]]:
    sent: list[tuple[str, str, Any]] = []

    def api(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30.0) -> tuple[int, Any]:
        sent.append((method, path, body))
        return answer

    monkeypatch.setattr(cli, "api", api)
    return sent


def test_status_renders_a_sandbox_open_view(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _api(monkeypatch, (200, {"mode": "sandbox_open", "default": "open", "previous": None}))
    result = CliRunner().invoke(cli.app, ["egress", "status"])
    assert result.exit_code == 0, result.output
    assert sent == [("GET", "/v1/control/egress-mode", None), ("GET", "/v1/control/implement-network", None)]
    assert "sandbox_open" in result.output and "open" in result.output


def test_status_shows_the_implement_network(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = {"/v1/control/egress-mode": {"mode": "open", "default": "open", "previous": None},
               "/v1/control/implement-network": {"value": "auto", "active": False, "egress_mode": "open",
                                                 "previous": None}}  # fmt: skip

    def api(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30.0) -> tuple[int, Any]:
        return 200, answers[path]

    monkeypatch.setattr(cli, "api", api)
    result = CliRunner().invoke(cli.app, ["egress", "status"])
    assert result.exit_code == 0, result.output
    assert '"implement_network": "auto"' in result.output and '"implement_network_active": false' in result.output


@pytest.mark.parametrize("known", [True, False])
def test_status_shows_the_public_floor_as_a_count(monkeypatch: pytest.MonkeyPatch, known: bool) -> None:
    """ADR-0025 (build-env design §4.7): known, since and the entry count, never an address."""
    _api(monkeypatch, (200, {"mode": "sandbox_full", "default": "open", "previous": None}))
    status = public_floor.FloorStatus(known, None if known else 1_792_600_300.0, 2 if known else 0, 7, True)
    monkeypatch.setattr(cli.public_floor, "read_state", lambda: status)
    result = CliRunner().invoke(cli.app, ["egress", "status"])
    assert result.exit_code == 0, result.output
    shown = json.loads(result.output)["public_floor"]
    assert shown == {"known": known, "since": status.since, "entries": status.entries}


def test_implement_network_needs_a_valid_value_and_the_tty_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    view = {"value": "on_request", "active": False, "egress_mode": "open", "previous": "off"}
    sent = _api(monkeypatch, (200, view))
    bad = CliRunner().invoke(cli.app, ["egress", "implement-network", "on"])
    assert bad.exit_code == 2 and "off|on_request|auto" in bad.output and sent == []
    refused = CliRunner().invoke(cli.app, ["egress", "implement-network", "on_request"])  # no TTY
    assert refused.exit_code == 2 and "interactive TTY confirm" in refused.output and sent == []
    words: list[str] = []
    monkeypatch.setattr(cli, "_confirm", words.append)
    ok = CliRunner().invoke(cli.app, ["egress", "implement-network", "on_request"])
    assert ok.exit_code == 0, ok.output
    assert words == ["IMPLEMENT NETWORK ON_REQUEST"]
    assert sent == [("POST", "/v1/control/implement-network", {"value": "on_request"})]
    _api(monkeypatch, (403, {"error": "forbidden"}))
    assert CliRunner().invoke(cli.app, ["egress", "implement-network", "auto"]).exit_code == 1


def test_mode_sandbox_open_needs_the_tty_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    view = {"mode": "sandbox_open", "default": "open", "previous": "open"}
    sent = _api(monkeypatch, (200, view))
    refused = CliRunner().invoke(cli.app, ["egress", "mode", "sandbox_open"])  # CliRunner's stdin is not a TTY
    assert refused.exit_code == 2 and "interactive TTY confirm" in refused.output and sent == []
    words: list[str] = []
    monkeypatch.setattr(cli, "_confirm", words.append)
    ok = CliRunner().invoke(cli.app, ["egress", "mode", "sandbox_open"])
    assert ok.exit_code == 0, ok.output
    assert words == ["EGRESS SANDBOX_OPEN"]
    assert sent == [("POST", "/v1/control/egress-mode", {"mode": "sandbox_open"})]


def test_unknown_mode_lists_all_five(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _api(monkeypatch, (200, {}))
    monkeypatch.setattr(cli, "_confirm", lambda word: None)
    bad = CliRunner().invoke(cli.app, ["egress", "mode", "sandbox-open"])
    assert bad.exit_code == 2 and sent == []
    assert "allowlist|research|open|sandbox_open|sandbox_full" in bad.output


CID = "c_01ABCDEFGH"


def _sbx(monkeypatch: pytest.MonkeyPatch, answers: dict[tuple[str, str], tuple[int, Any]]) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def sandboxd(method: str, path: str) -> tuple[int, Any]:
        calls.append((method, path))
        return answers.get((method, path), (0, "sandboxd unreachable"))

    monkeypatch.setattr(cli, "_sandboxd_status", sandboxd)
    return calls


def test_holds_lists_gatekeeper_holds_and_sandboxd_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _api(monkeypatch, (200, [{"cid": CID, "reason": "egress_anomaly"}]))
    pending = [{"cid": "c_01ABCDEFGJ", "rejected_code": "404:unknown_cid"}]
    _sbx(monkeypatch, {("GET", "/v1/egress/pending"): (200, pending)})
    result = CliRunner().invoke(cli.app, ["egress", "holds"])
    assert result.exit_code == 0, result.output
    assert sent == [("GET", "/v1/holds?open=true", None)]
    doc = json.loads(result.output)
    assert doc["holds"][0]["cid"] == CID and doc["sandboxd_pending"][0]["rejected_code"] == "404:unknown_cid"


def test_release_needs_root_and_the_tty_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _api(monkeypatch, (200, {"cid": CID, "released_by": "uid:0/operator"}))
    calls = _sbx(monkeypatch, {("DELETE", f"/v1/egress/pending/{CID}"): (404, {"error": "no_pending_hold"})})
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert CliRunner().invoke(cli.app, ["egress", "release", CID]).exit_code == 2
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    refused = CliRunner().invoke(cli.app, ["egress", "release", CID])  # no TTY
    assert refused.exit_code == 2 and "interactive TTY confirm" in refused.output
    assert sent == [] and calls == []
    words: list[str] = []
    monkeypatch.setattr(cli, "_confirm", words.append)
    ok = CliRunner().invoke(cli.app, ["egress", "release", CID])
    assert ok.exit_code == 0, ok.output
    assert words == ["RESUME"]
    assert sent == [("POST", f"/v1/contributions/{CID}/hold/release", {})]
    assert calls == [("DELETE", f"/v1/egress/pending/{CID}")]


def test_release_clears_a_rejected_pending_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """M3: the gatekeeper refused the hold (no hold there), sandboxd still blocks the cid: release clears it."""
    _api(monkeypatch, (404, {"error": "no_open_hold"}))
    _sbx(monkeypatch, {("DELETE", f"/v1/egress/pending/{CID}"): (200, {"cid": CID, "cleared": True})})
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "_confirm", lambda word: None)
    ok = CliRunner().invoke(cli.app, ["egress", "release", CID])
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.output) == {"gatekeeper": "no open hold", "sandboxd": {"cid": CID, "cleared": True}}
    _sbx(monkeypatch, {("DELETE", f"/v1/egress/pending/{CID}"): (404, {"error": "no_pending_hold"})})
    nothing = CliRunner().invoke(cli.app, ["egress", "release", CID])
    assert nothing.exit_code == 1 and "nothing to release" in nothing.output
    assert CliRunner().invoke(cli.app, ["egress", "release", "not-a-cid"]).exit_code == 2


def test_jobs_reads_the_summaries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for run_id, cid, start in (("r1", CID, 2.0), ("r2", "c_01ABCDEFGJ", 1.0), ("r3", CID, 3.0)):
        (tmp_path / f"{run_id}.egress.json").write_text(json.dumps({"run_id": run_id, "cid": cid,
                                                                    "window": {"start": start}}))  # fmt: skip
    old = tmp_path / "r0.egress.json"
    old.write_text(json.dumps({"run_id": "r0", "cid": CID, "window": {"start": 0.0}}))
    os.utime(old, (time.time() - 30 * 86400,) * 2)
    (tmp_path / "r9.log").write_text("not a summary")
    monkeypatch.setattr(cli, "EGRESS_SUMMARIES", tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert CliRunner().invoke(cli.app, ["egress", "jobs"]).exit_code == 2
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    every = CliRunner().invoke(cli.app, ["egress", "jobs"])
    assert every.exit_code == 0, every.output
    assert [d["run_id"] for d in json.loads(every.output)] == ["r2", "r1", "r3"]
    one = CliRunner().invoke(cli.app, ["egress", "jobs", "--cid", CID, "--days", "60"])
    assert [d["run_id"] for d in json.loads(one.output)] == ["r0", "r1", "r3"]


def test_help_texts_list_the_five_modes() -> None:
    assert EGRESS_MODES == ("allowlist", "research", "open", "sandbox_open", "sandbox_full")
    env = {"COLUMNS": "200", "TERMINAL_WIDTH": "200"}
    group = CliRunner().invoke(cli.app, ["egress", "--help"], env=env)
    mode = CliRunner().invoke(cli.app, ["egress", "mode", "--help"], env=env)
    assert group.exit_code == 0 and mode.exit_code == 0
    assert "allowlist | research | open | sandbox_open | sandbox_full" in mode.output
    assert "sandbox_open also gives fetch/validate web_public" in " ".join(mode.output.split())
    top = CliRunner().invoke(cli.app, ["--help"], env=env)
    assert "egress mode (allowlist | research | open | sandbox_open | sandbox_full)" in " ".join(top.output.split())
