#!/usr/bin/env python3
"""ossbot-github-mcp: the read-only GitHub MCP server for scout, self-review and Hermes
(phase2-build-plan "GitHub MCP server, read-only"; TRACEABILITY GHMCP-1..4).

Starts the pinned ``github-mcp-server`` with fixed flags (``stdio --read-only --toolsets
repos,issues,pull_requests,actions --tools get_me``; nobody can add flags) and the read token, after
one startup check: a first instance is asked for its tool list (``initialize`` + ``tools/list``), and
any tool that does not declare ``readOnlyHint: true``, or whose name has a write verb, refuses the
start (exit 3, fail closed: the client then has no GitHub tools at all). Then the server is exec'd
for the MCP client (kiro-cli or Hermes), which talks to it directly.

Token: ``GITHUB_READ_TOKEN`` from the environment, else the file ``$HOME/.ossbot/github-read-token``
(the sandbox entrypoint writes it there and drops the variable). Only ``GITHUB_PERSONAL_ACCESS_TOKEN``,
``PATH``, ``HOME``, ``LANG`` and the proxy variables reach the server. No token: exit 2.

``--check`` prints the startup check as JSON; ``--doctor OWNER/REPO`` also calls ``get_me`` and
lists the repository's root (doctor's live read check) and prints the login and entry count only.
Stdlib only; baked into ``sandbox-llm`` and installed on the host at /opt/ossbot/bin/ossbot-github-mcp.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import IO, Any

SERVER_BIN = "/opt/ossbot/bin/github-mcp-server"
TOOLSETS = "repos,issues,pull_requests,actions"
EXTRA_TOOLS = "get_me"  # context toolset, read-only: doctor's identity check
TOKEN_FILE = ".ossbot/github-read-token"  # noqa: S105 - a path under $HOME, not a value
PROTOCOL_VERSION = "2025-06-18"
MAX_LINE = 8 * 1024 * 1024
MAX_PAGES = 20
PASS_ENV = ("PATH", "HOME", "LANG", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")
TOKEN_RE = re.compile(r"^(github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})$")
# Defence in depth next to readOnlyHint: verbs no read tool of the pinned release uses.
WRITE_VERB = re.compile(
    r"(^|_)(create|update|delete|remove|add|merge|push|fork|submit|assign|unassign|rerun|cancel|dismiss|lock|"
    r"unlock|star|unstar|follow|unfollow|approve|trigger|write|edit|close|reopen|set|mark)(_|$)"
)


class CheckError(RuntimeError):
    pass


def server_argv(server_bin: str = SERVER_BIN) -> list[str]:
    return [server_bin, "stdio", "--read-only", "--toolsets", TOOLSETS, "--tools", EXTRA_TOOLS]


def read_token(env: Mapping[str, str]) -> str | None:
    token = (env.get("GITHUB_READ_TOKEN") or "").strip()
    if not token and env.get("HOME"):
        try:
            with open(os.path.join(env["HOME"], TOKEN_FILE), encoding="utf-8") as fh:
                token = fh.read(512).strip()
        except OSError:
            token = ""
    return token if TOKEN_RE.match(token) else None


def server_env(env: Mapping[str, str], token: str) -> dict[str, str]:
    out = {k: env[k] for k in PASS_ENV if env.get(k)}
    out.setdefault("PATH", "/usr/bin:/bin")
    out["GITHUB_PERSONAL_ACCESS_TOKEN"] = token
    return out


def refused_tools(tools: Sequence[Any]) -> list[str]:
    """Names (or ``<malformed>``) of the listed tools that may not be exposed."""
    bad: list[str] = []
    for tool in tools:
        name = tool.get("name") if isinstance(tool, dict) else None
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
            bad.append("<malformed>")
            continue
        notes = tool.get("annotations")
        read_only = isinstance(notes, dict) and notes.get("readOnlyHint") is True
        if not read_only or WRITE_VERB.search(name):
            bad.append(name)
    return bad


class Session:
    """A minimal MCP client over a child's stdio (newline-delimited JSON-RPC)."""

    def __init__(self, argv: Sequence[str], env: Mapping[str, str], timeout: float = 30.0) -> None:
        self.proc = subprocess.Popen(
            list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=dict(env)
        )
        self.timeout = timeout
        self._id = 0

    def _send(self, doc: dict[str, Any]) -> None:
        stdin = self.proc.stdin
        assert stdin is not None
        stdin.write(json.dumps(doc).encode() + b"\n")
        stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        want = self._id
        self._send({"jsonrpc": "2.0", "id": want, "method": method, "params": params or {}})
        stdout = self.proc.stdout
        assert stdout is not None
        while True:
            line = _read_line(stdout)
            if line is None:
                raise CheckError(f"server closed during {method}")
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if not isinstance(doc, dict) or doc.get("id") != want:
                continue  # notifications and logging
            if "error" in doc:
                raise CheckError(f"{method}: JSON-RPC error {str(doc['error'])[:200]}")
            result = doc.get("result")
            if not isinstance(result, dict):
                raise CheckError(f"{method}: no result")
            return result

    def initialize(self) -> None:
        self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                    "clientInfo": {"name": "ossbot-github-mcp-check", "version": "1"}})  # fmt: skip
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def list_tools(self) -> list[Any]:
        tools: list[Any] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            result = self.request("tools/list", {"cursor": cursor} if cursor else {})
            page = result.get("tools")
            if not isinstance(page, list):
                raise CheckError("tools/list: no tool list")
            tools += page
            nxt = result.get("nextCursor")
            if not isinstance(nxt, str) or not nxt:
                return tools
            cursor = nxt
        raise CheckError("tools/list: too many pages")

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise CheckError(f"{name} returned an error")
        for part in result.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                try:
                    return json.loads(part["text"])
                except ValueError:
                    return part["text"]
        return result.get("structuredContent")

    def close(self) -> None:
        for stream in (self.proc.stdin, self.proc.stdout):
            if stream is not None:
                stream.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


def _read_line(stream: IO[bytes]) -> bytes | None:
    line = stream.readline(MAX_LINE + 1)
    if not line:
        return None
    if len(line) > MAX_LINE:
        raise CheckError("server line over the size limit")
    return line


def check(argv: Sequence[str], env: Mapping[str, str]) -> dict[str, Any]:
    """The startup check: ``{"ok", "tools", "refused"}`` (``refused`` names never include values)."""
    session = Session(argv, env)
    try:
        session.initialize()
        tools = session.list_tools()
    finally:
        session.close()
    refused = refused_tools(tools)
    return {"ok": bool(tools) and not refused, "tools": len(tools), "refused": refused[:50]}


def doctor(argv: Sequence[str], env: Mapping[str, str], repo: str) -> dict[str, Any]:
    owner, _, name = repo.partition("/")
    session = Session(argv, env)
    try:
        session.initialize()
        refused = refused_tools(session.list_tools())
        if refused:
            return {"ok": False, "refused": refused[:50]}
        me = session.call("get_me", {})
        listing = session.call("get_file_contents", {"owner": owner, "repo": name, "path": "/"})
    finally:
        session.close()
    login = me.get("login") if isinstance(me, dict) else None
    entries = len(listing) if isinstance(listing, list) else 0
    return {"ok": isinstance(login, str) and entries > 0, "login": login if isinstance(login, str) else None,
            "entries": entries}  # fmt: skip


def main(args: Sequence[str] | None = None, *, server_bin: str = SERVER_BIN) -> int:
    args = list(sys.argv[1:] if args is None else args)
    token = read_token(os.environ)
    if token is None:
        print("ossbot-github-mcp: no read token (GITHUB_READ_TOKEN)", file=sys.stderr)
        return 2
    argv, env = server_argv(server_bin), server_env(os.environ, token)
    try:
        if args[:1] == ["--doctor"] and len(args) == 2 and re.fullmatch(r"[\w.-]+/[\w.-]+", args[1]):
            out = doctor(argv, env, args[1])
            print(json.dumps(out))
            return 0 if out["ok"] else 3
        verdict = check(argv, env)
    except (CheckError, OSError) as exc:
        print(f"ossbot-github-mcp: startup check failed: {exc}", file=sys.stderr)
        return 3
    if args == ["--check"]:
        print(json.dumps(verdict))
        return 0 if verdict["ok"] else 3
    if args:
        print("usage: ossbot-github-mcp [--check | --doctor OWNER/REPO]", file=sys.stderr)
        return 2
    if not verdict["ok"]:
        print(f"ossbot-github-mcp: refusing to start, not read-only: {verdict['refused']}", file=sys.stderr)
        return 3
    os.execve(argv[0], argv, env)  # noqa: S606 - fixed argv, no shell
    return 0  # pragma: no cover - execve does not return


if __name__ == "__main__":
    sys.exit(main())
