"""ossbot-exec-mcp relay (DESIGN §10.4 M4): four tools, connect retry, symlinked socket refused."""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from ossbot.sandbox_tools import exec_mcp, execd

RELAY = Path(exec_mcp.__file__)


def _server(tmp_path: Path) -> str:
    repo = tmp_path / "work" / "repo"
    repo.mkdir(parents=True)
    (repo / "a.txt").write_text("hello")
    os.symlink("/proc/self/environ", repo / "env")
    sock_path = str(tmp_path / "exec.sock")
    server = execd.bind(sock_path)
    executor = execd.Executor(str(repo), str(tmp_path / "work"))
    threading.Thread(target=execd.serve, args=(server, executor, None), daemon=True).start()
    return sock_path


def _session(relay: exec_mcp.Relay, *messages: dict[str, Any]) -> list[dict[str, Any]]:
    stdin = io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in messages))
    stdout = io.BytesIO()
    exec_mcp.serve(stdin, stdout, relay)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def test_exposes_exactly_four_tools(tmp_path: Path) -> None:
    relay = exec_mcp.Relay(exec_mcp.connect(_server(tmp_path), total_s=2))
    replies = _session(
        relay,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[0]["result"]["serverInfo"]["name"] == "ossbot-exec"
    assert sorted(t["name"] for t in replies[1]["result"]["tools"]) == ["list_dir", "read_file", "run", "write_file"]


def test_tool_calls_relay_to_execd(tmp_path: Path) -> None:
    relay = exec_mcp.Relay(exec_mcp.connect(_server(tmp_path), total_s=2))
    replies = _session(
        relay,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "a.txt"}},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "env"}},
        },
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "shell", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "run", "arguments": {"cmd": "echo hi"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "resources/list"},
    )
    ok = json.loads(replies[0]["result"]["content"][0]["text"])
    assert ok["result"]["content"] == "hello" and replies[0]["result"]["isError"] is False
    assert replies[1]["result"]["isError"] is True and replies[1]["result"]["content"][0]["text"].startswith("ELINK")
    assert replies[2]["error"]["code"] == -32602
    assert json.loads(replies[3]["result"]["content"][0]["text"])["result"]["stdout"] == "hi\n"
    assert replies[4]["error"]["code"] == -32601


def test_symlink_at_socket_path_refused(tmp_path: Path) -> None:
    real = _server(tmp_path)
    link = tmp_path / "link.sock"
    link.symlink_to(real)
    started = time.monotonic()
    with pytest.raises(exec_mcp.RelayError, match="symlink"):
        exec_mcp.connect(str(link), total_s=5)
    assert time.monotonic() - started < 1  # refused at once, not retried


def test_regular_file_at_socket_path_never_connects(tmp_path: Path) -> None:
    path = tmp_path / "exec.sock"
    path.write_text("not a socket")
    with pytest.raises(exec_mcp.RelayError):
        exec_mcp.connect(str(path), total_s=0.6, interval_s=0.1)


def test_retries_until_the_socket_appears(tmp_path: Path) -> None:
    sock_path = str(tmp_path / "late.sock")

    def late() -> None:
        time.sleep(0.6)
        server = socket.socket(socket.AF_UNIX)
        server.bind(sock_path)
        server.listen(1)
        server.accept()

    threading.Thread(target=late, daemon=True).start()
    exec_mcp.connect(sock_path, total_s=5, interval_s=0.25).close()


def test_gives_up_after_the_deadline_and_exits_nonzero(tmp_path: Path) -> None:
    started = time.monotonic()
    with pytest.raises(exec_mcp.RelayError):
        exec_mcp.connect(str(tmp_path / "missing.sock"), total_s=1, interval_s=0.25)
    assert 0.9 < time.monotonic() - started < 3
    assert exec_mcp.RETRY_INTERVAL_S == 0.25 and exec_mcp.RETRY_TOTAL_S == 30.0


def test_main_exit_code_for_symlinked_socket(tmp_path: Path) -> None:
    link = tmp_path / "s.sock"
    link.symlink_to(tmp_path / "nowhere")
    proc = subprocess.run(
        [sys.executable, str(RELAY), str(link)], capture_output=True, check=False, timeout=10, input=b""
    )
    assert proc.returncode == 3
    assert b"symlink" in proc.stderr


def test_relay_is_stdlib_only() -> None:
    for module in (exec_mcp, execd):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "import ossbot" not in source and "from ossbot" not in source
