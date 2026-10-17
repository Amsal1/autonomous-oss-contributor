#!/usr/bin/env python3
"""ossbot-exec-mcp: stdio MCP server spawned by kiro-cli in ``agent-llm`` (DESIGN §10.4, M4).

Relays exactly four tools (``read_file``, ``write_file``, ``list_dir``, ``run``) to ossbot-execd
over ``/run/exec/exec.sock``. Before each connect attempt it ``lstat``s the socket path, which
must be a socket and not a symlink (a symlink is refused at once). It retries every 250 ms for
up to 30 s and exits non-zero on failure, so kiro-cli (``--require-mcp-startup``) exits 3.
Stdlib only; baked into ``sandbox-llm`` at /opt/ossbot/bin/ossbot-exec-mcp. It never reads the
socket volume or anything else from disk: responses are data for the (untrusted) model.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
import time
from typing import Any, BinaryIO

SERVER_NAME = "ossbot-exec"
PROTOCOL_VERSION = "2025-06-18"
RETRY_INTERVAL_S = 0.25
RETRY_TOTAL_S = 30.0
MAX_LINE = 4 * 1024 * 1024

TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read a file under /work/repo (path relative to the repo). Returns at most 256 KB.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 262144},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_file",
        "description": "Replace a regular file under /work/repo with content (at most 1 MB).",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_dir",
        "description": "List a directory under /work/repo (depth <= 3, <= 2000 entries; links are not followed).",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "depth": {"type": "integer", "minimum": 1, "maximum": 3}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run",
        "description": "Run a shell command offline in /work/repo (timeout_s <= 1200; output capped at 64 KB).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string"},
                "cwd": {"type": "string"},
                "timeout_s": {"type": "integer", "minimum": 1, "maximum": 1200},
            },
            "required": ["cmd"],
            "additionalProperties": False,
        },
    },
]
TOOL_NAMES = frozenset(t["name"] for t in TOOLS)


class RelayError(Exception):
    pass


def connect(path: str, total_s: float = RETRY_TOTAL_S, interval_s: float = RETRY_INTERVAL_S) -> socket.socket:
    deadline = time.monotonic() + total_s
    while True:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            st = None
        if st is not None:
            if stat.S_ISLNK(st.st_mode):
                raise RelayError(f"{path} is a symlink; refusing")
            if stat.S_ISSOCK(st.st_mode):
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    sock.connect(path)
                    return sock
                except OSError:
                    sock.close()
        if time.monotonic() >= deadline:
            raise RelayError(f"could not connect to {path} within {total_s:.0f} s")
        time.sleep(interval_s)


class Relay:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.reader = sock.makefile("rb")
        self.seq = 0

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        self.seq += 1
        self.sock.sendall((json.dumps({"id": self.seq, "op": op, "args": args}) + "\n").encode("utf-8"))
        line = self.reader.readline(MAX_LINE)
        if not line:
            raise RelayError("execd closed the connection")
        doc = json.loads(line)
        if not isinstance(doc, dict):
            raise RelayError("bad execd response")
        return doc


def _result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle(message: Any, relay: Relay) -> dict[str, Any] | None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "invalid request")
    method, req_id, params = message.get("method"), message.get("id"), message.get("params") or {}
    if "id" not in message:  # notification (e.g. notifications/initialized)
        return None
    if method == "initialize":
        version = params.get("protocolVersion") if isinstance(params, dict) else None
        return _result(req_id, {
            "protocolVersion": version if isinstance(version, str) else PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": "1"},
        })  # fmt: skip
    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name") if isinstance(params, dict) else None
        arguments = params.get("arguments", {}) if isinstance(params, dict) else None
        if name not in TOOL_NAMES or not isinstance(arguments, dict):
            return _error(req_id, -32602, "unknown tool or bad arguments")
        response = relay.call(str(name), arguments)
        if response.get("ok"):
            payload = {"result": response.get("result"), "truncated": bool(response.get("truncated"))}
            return _result(req_id, {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False})
        error = response.get("error") or {}
        text = f"{error.get('code', 'EINVAL')}: {error.get('message', '')}"
        return _result(req_id, {"content": [{"type": "text", "text": text}], "isError": True})
    return _error(req_id, -32601, f"method not found: {method}")


def serve(stdin: BinaryIO, stdout: BinaryIO, relay: Relay) -> None:
    for line in iter(lambda: stdin.readline(MAX_LINE), b""):
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply: dict[str, Any] | None = _error(None, -32700, "parse error")
        else:
            reply = handle(message, relay)
        if reply is not None:
            stdout.write((json.dumps(reply) + "\n").encode("utf-8"))
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = args[0] if args else "/run/exec/exec.sock"
    try:
        relay = Relay(connect(path))
    except RelayError as exc:
        print(f"ossbot-exec-mcp: {exc}", file=sys.stderr)
        return 3
    try:
        serve(sys.stdin.buffer, sys.stdout.buffer, relay)
    except (RelayError, OSError) as exc:
        print(f"ossbot-exec-mcp: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
