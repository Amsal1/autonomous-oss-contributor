"""``ossctl drain-listen`` (ADR-0021 addendum post-v1): the fixed stop-only grammar, the ``down``
path, EOF, the root-only start and a drain that survives SIGHUP and a closed stdin."""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from ossbot.ctl import cli, drain_listener
from ossbot.ctl.drain_listener import Down, Ping, parse

REPO = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("line", "want"),
    [
        (b"ping", Ping()),
        (b"down 90 blocker", Down(90, "blocker")),
        (b"down 0 x", Down(0, "x")),
        (b"down 120 " + b"a" * 40, Down(120, "a" * 40)),
        (b"down 7 blocker_test-2", Down(7, "blocker_test-2")),
    ],
)
def test_grammar_accepts(line: bytes, want: object) -> None:
    assert parse(line) == want


@pytest.mark.parametrize(
    "line",
    [
        b"",
        b"PING",
        b"ping ",
        b" ping",
        b"ping extra",
        b"down",
        b"down 90",
        b"down 121 blocker",
        b"down 999 blocker",
        b"down -1 blocker",
        b"down 090 blocker",
        b"down 9a blocker",
        b"down 90 blocker extra",
        b"down  90 blocker",
        b"down 90 " + b"a" * 41,
        b"down 90 bad;caller",
        b"down 90 caller$x",
        b"down\t90\tblocker",
        b"down 90 bl\xc3\xa9",
        b"\x00ping",
        b"x" * 65,
        b"up",
        b"up 90 blocker",
        b"resume",
        b"dryrun off",
        b"dryrun on",
        b"approve 1",
        b"reload",
        b"kill",
        b"pause",
        b"\xff\xfe",
    ],
)
def test_grammar_refuses(line: bytes) -> None:
    assert parse(line) is None


class _Recorder:
    def __init__(self, code: int = 0) -> None:
        self.calls: list[tuple[int, str, str]] = []
        self.code = code

    def __call__(self, timeout_s: int, kind: str, scope: str) -> int:
        self.calls.append((timeout_s, kind, scope))
        return self.code


def _serve(data: bytes, run_down: Any, state: str = "up") -> tuple[int, list[str], list[str]]:
    out = io.BytesIO()
    logs: list[str] = []
    detached: list[bool] = []
    code = drain_listener.serve(
        io.BytesIO(data), out, run_down, lambda: state, logs.append, detach=lambda: detached.append(True)
    )
    if detached:
        logs.append("detached")
    return code, out.getvalue().decode("ascii").splitlines(), logs


def test_ping_and_eof() -> None:
    rec = _Recorder()
    code, out, logs = _serve(b"ping\n", rec, state="draining")
    assert code == 0 and out == ["pong draining"] and rec.calls == []
    assert logs[-1] == "stdin closed; exiting"


def test_down_runs_run_down_once_and_reports() -> None:
    rec = _Recorder(code=0)
    code, out, logs = _serve(b"down 90 blocker\n", rec)
    assert code == 0 and rec.calls == [(90, "graceful", "distro")]
    assert out[0] == "ack"
    assert out[1].startswith("drained exit=0 elapsed=") and len(out) == 2
    assert "detached" in logs


def test_down_failure_is_reported_not_raised() -> None:
    def boom(timeout_s: int, kind: str, scope: str) -> int:
        raise RuntimeError("gatekeeper gone")

    code, out, _ = _serve(b"down 5 t\n", boom)
    assert code == 0 and out[0] == "ack" and out[1].startswith("drained exit=1 ")


def test_refused_lines_answer_error_and_are_ignored() -> None:
    rec = _Recorder()
    data = b"resume\nup\n" + b"y" * 300 + b"\ndown 500 blocker\nping\n"
    _, out, _ = _serve(data, rec)
    assert out == ["error grammar"] * 4 + ["pong up"]
    assert rec.calls == []


def test_crlf_and_a_leading_bom_are_accepted() -> None:
    rec = _Recorder()
    _, out, _ = _serve(b"\xef\xbb\xbfping\r\ndown 3 blocker\r\n", rec)
    assert out[0] == "pong up" and out[1] == "ack" and rec.calls == [(3, "graceful", "distro")]


def test_bom_only_tolerated_on_the_first_line() -> None:
    _, out, _ = _serve(b"ping\n\xef\xbb\xbfping\n", _Recorder())
    assert out == ["pong up", "error grammar"]


def test_closed_output_after_drain_ends_the_listener() -> None:
    class Broken(io.BytesIO):
        def write(self, b: Any) -> int:
            raise BrokenPipeError

    rec = _Recorder()
    code = drain_listener.serve(
        io.BytesIO(b"down 5 blocker\nping\n"), Broken(), rec, lambda: "up", lambda _t: None, detach=lambda: None
    )
    assert code == 0 and rec.calls == [(5, "graceful", "distro")]


def test_refuses_to_start_unless_root(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert drain_listener.main() == 2
    assert "root only" in capsys.readouterr().err


def test_no_flag_writes_besides_run_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """serve() itself never writes a control flag: every write goes through run_down."""
    writes: list[Any] = []
    monkeypatch.setattr(cli, "write_flag", lambda *a, **k: writes.append(a))
    _serve(b"ping\ndown 1 blocker\nresume\n", _Recorder())
    assert writes == []


def test_cli_down_calls_run_down_with_the_same_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    seen: list[tuple[Any, ...]] = []

    def fake(timeout: int | None, kind: str, scope: str) -> tuple[int, dict[str, Any]]:
        seen.append((timeout, kind, scope))
        return 0, {"scope": scope}

    monkeypatch.setattr(cli, "run_down", fake)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    r = CliRunner().invoke(cli.app, ["down", "--timeout", "20", "--kind", "hook"])
    assert r.exit_code == 0, r.output
    r = CliRunner().invoke(cli.app, ["down"])
    assert r.exit_code == 0, r.output
    assert seen == [(20, "hook", "distro"), (None, "graceful", "distro")]
    assert CliRunner().invoke(cli.app, ["down", "--kind", "bogus"]).exit_code == 2


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="POSIX only")
def test_drain_survives_sighup_and_closed_stdin(tmp_path: Path) -> None:
    record = tmp_path / "ran"
    env = {**os.environ, "PYTHONPATH": str(REPO / "src") + os.pathsep + str(REPO)}
    child = subprocess.Popen(
        [sys.executable, str(REPO / "tests" / "unit" / "ctl" / "drain_child.py"), str(record)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=env,
    )
    assert child.stdin is not None and child.stdout is not None
    child.stdin.write(b"down 5 blocker\n")
    child.stdin.flush()
    assert child.stdout.readline() == b"ack\n"
    child.send_signal(signal.SIGHUP)
    child.stdin.close()
    time.sleep(0.1)
    assert child.poll() is None  # still draining
    assert child.stdout.readline().startswith(b"drained exit=0 ")
    assert child.wait(timeout=10) == 0
    assert record.read_text(encoding="ascii") == "5 graceful distro\n"
