"""Scripted fake of the Kiro CLI V3 ACP server (stdio NDJSON JSON-RPC 2.0), DESIGN §22.3.

Run as ``python fake_acp_v3.py <scenario.json>``. Every frame the client sends is appended to the
scenario's ``log`` file (one JSON object per line) so tests can assert what the adapter did. Scenario
keys (all optional):

* ``init``: the ``initialize`` result (default: image prompts, a few extension methods);
* ``session``: ``mode`` (``ossbot-chat``), ``tools`` (advertised list or ``null``), ``models`` (ids of
  the ``model`` config option), ``current_model``, ``honour_meta_model`` (apply ``_meta.kiro.modelId``),
  ``efforts``, ``current_effort``, ``no_mode`` (omit ``modes``);
* ``load``: the same keys for ``session/load``;
* ``prompts``: one action list per ``session/prompt`` call (the last one repeats). Actions:
  ``{"text": str}``, ``{"thought": str}``, ``{"tool_call": {...}}``, ``{"permission": [options]}``,
  ``{"token": true}`` (asks ``_kiro/auth/getAccessToken``), ``{"request": method}`` (any agent-to-client
  request), ``{"mode_update": id}``, ``{"tools_changed": [ids]}``, ``{"drop": true}`` (exit without a
  response), ``{"sleep": s}``, ``{"wait_cancel": true}`` (block until ``session/cancel``), ``{"raw": str}``
  (write a raw line).
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

DEFAULT_INIT = {
    "protocolVersion": 1,
    "agentCapabilities": {
        "loadSession": True,
        "promptCapabilities": {"image": True, "audio": False, "embeddedContext": True},
        "_meta": {"kiro": {"extensionMethods": ["_kiro/session/context", "_kiro/account/getUsage"]}},
    },
    "authMethods": [],
}


class Fake:
    def __init__(self, scenario: dict[str, Any]) -> None:
        self.s = scenario
        self.log_path = scenario.get("log")
        self.ids = iter(range(1000, 1 << 30))
        self.sessions: dict[str, dict[str, Any]] = {}
        self.prompt_count = 0
        self.cancelled = False

    # ------------------------------------------------------------------ io
    def record(self, frame: Any) -> None:
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(frame) + "\n")

    def send(self, frame: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(frame) + "\n")
        sys.stdout.flush()

    def read(self) -> dict[str, Any] | None:
        line = sys.stdin.readline()
        if not line:
            return None
        msg = json.loads(line)
        self.record(msg)
        return msg

    def result(self, req_id: Any, result: Any) -> None:
        self.send({"jsonrpc": "2.0", "id": req_id, "result": result})

    def update(self, sid: str, update: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": update}})

    def ask(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """Agent-to-client request; reads until its response arrives (handling cancel in between)."""
        rid = next(self.ids)
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            msg = self.read()
            if msg is None:
                return None
            if msg.get("id") == rid and "method" not in msg:
                return msg
            if msg.get("method") == "session/cancel":
                self.cancelled = True

    # ------------------------------------------------------------------ sessions
    def session_result(self, sid: str, params: dict[str, Any], key: str) -> dict[str, Any]:
        cfg = {**self.s.get("session", {}), **(self.s.get(key, {}) if key == "load" else {})}
        meta = ((params.get("_meta") or {}).get("kiro")) or {}
        models = cfg.get("models", ["claude-sonnet-5.5", "claude-opus-5.5", "claude-haiku-4.5"])
        current = cfg.get("current_model", "auto")
        if cfg.get("honour_meta_model") and meta.get("modelId") in models:
            current = meta["modelId"]
        efforts = cfg.get("efforts", ["low", "medium", "high"])
        current_effort = cfg.get("current_effort", "medium")
        if cfg.get("honour_meta_model") and meta.get("effortLevel") in efforts:
            current_effort = meta["effortLevel"]
        state = self.sessions.setdefault(sid, {})
        state.update(model=current, effort=current_effort)
        res: dict[str, Any] = {
            "sessionId": sid,
            "configOptions": [
                {
                    "id": "model",
                    "name": "Model",
                    "category": "model",
                    "type": "select",
                    "currentValue": current,
                    "options": [{"value": m, "name": m} for m in [*models, "auto"]],
                },
                {
                    "id": "effortLevel",
                    "name": "Effort",
                    "type": "select",
                    "currentValue": current_effort,
                    "options": [{"value": e, "name": e} for e in efforts],
                },
            ],
        }
        if not cfg.get("no_mode"):
            mode = cfg.get("mode", "ossbot-chat")
            res["modes"] = {"currentModeId": mode, "availableModes": [{"id": mode, "name": mode}]}
        if cfg.get("tools") is not None:
            res["tools"] = cfg["tools"]
        return res

    def run_prompt(self, req_id: Any, params: dict[str, Any]) -> bool:
        sid = params.get("sessionId", "")
        scripts = self.s.get("prompts") or [[{"text": "ok"}]]
        index = self.prompt_count
        if self.log_path:  # count across restarted processes (the log is shared)
            with open(self.log_path, encoding="utf-8") as fh:
                index = sum(1 for line in fh if '"session/prompt"' in line) - 1
        actions = scripts[min(max(index, 0), len(scripts) - 1)]
        self.prompt_count += 1
        self.cancelled = False
        for act in actions:
            if "text" in act:
                self.update(
                    sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": act["text"]}}
                )
            elif "thought" in act:
                self.update(
                    sid, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": act["thought"]}}
                )
            elif "tool_call" in act:
                self.update(sid, {"sessionUpdate": "tool_call", **act["tool_call"]})
            elif "permission" in act:
                self.ask(
                    "session/request_permission",
                    {
                        "sessionId": sid,
                        "toolCall": {"toolCallId": "call-1", "title": "Run", "status": "pending"},
                        "options": act["permission"],
                        "_meta": {
                            "kiro": {"consent": {"capability": "shell", "resource": "id", "persistableConsent": True}}
                        },
                    },
                )
            elif "token" in act:
                self.ask("_kiro/auth/getAccessToken", {})
            elif "request" in act:
                self.ask(act["request"], {"sessionId": sid, "path": "/etc/passwd"})
            elif "mode_update" in act:
                self.update(sid, {"sessionUpdate": "current_mode_update", "currentModeId": act["mode_update"]})
            elif "tools_changed" in act:
                self.send(
                    {
                        "jsonrpc": "2.0",
                        "method": "_kiro/tools/didChange",
                        "params": {"sessionId": sid, "tools": act["tools_changed"]},
                    }
                )
            elif "drop" in act:
                return False
            elif "sleep" in act:
                time.sleep(float(act["sleep"]))
            elif "raw" in act:
                sys.stdout.write(act["raw"] + "\n")
                sys.stdout.flush()
            elif "wait_cancel" in act:
                while not self.cancelled:
                    msg = self.read()
                    if msg is None:
                        return False
                    if msg.get("method") == "session/cancel":
                        self.cancelled = True
                self.result(req_id, {"stopReason": "cancelled"})
                return True
        self.result(req_id, {"stopReason": "cancelled" if self.cancelled else "end_turn"})
        return True

    # ------------------------------------------------------------------ main loop
    def serve(self) -> None:
        n = 0
        while True:
            msg = self.read()
            if msg is None:
                return
            method, req_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
            if method is None or req_id is None:
                continue  # responses to our requests outside a prompt, notifications (cancel)
            if method == "initialize":
                self.result(req_id, self.s.get("init", DEFAULT_INIT))
            elif method == "session/new":
                n += 1
                self.result(req_id, self.session_result(f"sess-{n}", params, "new"))
            elif method == "session/load":
                self.result(req_id, self.session_result(params.get("sessionId", "sess-x"), params, "load"))
            elif method == "session/set_config_option":
                self.result(req_id, {"configOptions": []})
            elif method == "session/prompt":
                if not self.run_prompt(req_id, params):
                    return
            else:
                self.send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"no {method}"}})


def main() -> None:
    with open(sys.argv[1], encoding="utf-8") as fh:
        scenario = json.load(fh)
    Fake(scenario).serve()


if __name__ == "__main__":
    main()
