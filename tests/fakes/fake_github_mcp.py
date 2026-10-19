"""Fake ``github-mcp-server stdio`` for the ossbot-github-mcp startup check (GHMCP-3).

``python fake_github_mcp.py <scenario.json> <server argv...>``. Scenario keys: ``pages`` (lists of tool
dicts, one per ``tools/list`` page), ``login``, ``entries`` (root listing length), ``log`` (a file that
gets one JSON line: the argv and the environment *names*, plus whether the token variable was set).
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    with open(sys.argv[1], encoding="utf-8") as fh:
        scenario = json.loads(fh.read())
    if scenario.get("log"):
        with open(scenario["log"], "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "argv": sys.argv[2:],
                        "env": sorted(os.environ),
                        "token": bool(os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")),
                    }
                )
                + "\n"
            )
    pages = scenario.get("pages") or [[]]
    for raw in sys.stdin:
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if "id" not in msg:
            continue
        method, params = msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-github", "version": "0"},
            }
        elif method == "tools/list":
            index = int(params.get("cursor") or 0)
            result = {"tools": pages[index]}
            if index + 1 < len(pages):
                result["nextCursor"] = str(index + 1)
        elif method == "tools/call" and params.get("name") == "get_me":
            result = {"content": [{"type": "text", "text": json.dumps({"login": scenario.get("login", "octo")})}]}
        elif method == "tools/call" and params.get("name") == "get_file_contents":
            listing = [{"name": f"f{i}", "type": "file"} for i in range(scenario.get("entries", 3))]
            result = {"content": [{"type": "text", "text": json.dumps(listing)}]}
        else:
            print(
                json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "nope"}}),
                flush=True,
            )
            continue
        print(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}}), flush=True
        )
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
