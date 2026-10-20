"""Kiro CLI V3 ACP client core: launch, session, surface gate, permissions, NDJSON transport.

Stdlib only and free of Hermes imports, so a separate (e.g. privileged) health probe can import it from
its own copy without loading any Hermes code. The defaults are generic (the user's home, no proxy, mode
``hermes-chat``); deployments set their paths, proxy and expected mode through the keyword arguments
or the ``HERMES_KIRO_ACP_*`` environment of ``v3.V3Settings``. Behaviour follows the Kiro "Migrate an
ACP client to CLI V3" guide:

* launch ``kiro-cli acp --agent-engine=v3`` (``--auth-method=client`` when the client answers
  ``_kiro/auth/getAccessToken``); the v2-only flags ``--agent --model --effort --trust-*`` never appear;
* newline-delimited JSON-RPC 2.0; ``session/update``, ``session/request_permission`` and (client auth)
  ``_kiro/auth/getAccessToken`` are registered before ``session/new``; any other agent-to-client request
  with an id gets ``-32601``. The client advertises no capabilities, so no file or terminal bridge exists;
* optional calls are gated on what ``initialize`` advertises, never on a version;
* a session must report the expected mode and, where a tool list is advertised, only allowed tools;
  anything else is a surface violation: the session is not prompted and the violation hook runs;
* every permission request is answered with the first ``reject_once`` option, else ``reject_always``,
  else ``cancelled``; options are never synthesized and consent metadata is never returned;
* a turn completes only on the ``session/prompt`` response; after ``session/cancel`` (ACP prompt-turn
  cancellation) updates are still read, permission requests are answered ``cancelled``, and the wait
  for the response can be bounded (``CancelTimeout``);
* the access token answered to ``_kiro/auth/getAccessToken`` never reaches a log record.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import IO, Any

log = logging.getLogger("kiro_acp.v3")

DEFAULT_HOME = os.path.expanduser("~")
DEFAULT_KIRO_CLI = os.path.join(DEFAULT_HOME, ".local", "bin", "kiro-cli")  # where the Kiro installer puts it
DEFAULT_KIRO_CWD = os.path.join(DEFAULT_HOME, ".hermes", "kiro-cwd")  # empty directory, not a repository
DEFAULT_WORKSPACE = os.path.join(DEFAULT_HOME, ".hermes", "workspace")
DEFAULT_PROXY = ""  # no HTTPS_PROXY unless one is configured
EXPECTED_MODE = "hermes-chat"  # the custom Kiro agent every session must run
ALLOWED_TOOLS: frozenset[str] = frozenset({"web_search", "web_fetch"})
AUTH_METHODS = ("default", "client")
V2_ONLY_FLAGS = ("--agent", "--model", "--effort", "--trust-all-tools", "--trust-tools")
TOKEN_METHOD = "_kiro/auth/getAccessToken"
TOOLS_CHANGED = "_kiro/tools/didChange"
MAX_LINE = 8 * 1024 * 1024  # one JSON object per line, at most 8 MB
MAX_MALFORMED = 3  # malformed lines before the transport is closed (§20)
REDACTED = "[redacted]"
METHOD_NOT_FOUND = -32601
_CLOSED = "\x00closed"  # internal wake-up marker for waiters when the stream ends


class AcpError(RuntimeError):
    """Base class of every adapter error."""


class TransportClosed(AcpError):
    """The agent process went away (EOF, malformed stream or close)."""


class CancelTimeout(AcpError):
    """A cancelled turn's ``session/prompt`` response did not arrive within the grace period."""


class JsonRpcError(AcpError):
    def __init__(self, method: str, code: int, message: str) -> None:
        super().__init__(f"{method} failed ({code}): {message}")
        self.method, self.code, self.message = method, code, message


class SurfaceViolation(AcpError):
    """The session is not the expected agent mode or advertises a tool outside the allowlist."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"chat disabled: Kiro session surface check failed ({reason}: {detail})")
        self.reason, self.detail = reason, detail


# ------------------------------------------------------------------------------------ launch
@dataclass(frozen=True)
class LaunchSpec:
    argv: tuple[str, ...]
    cwd: str
    env: dict[str, str]


def launch_spec(
    *,
    auth_method: str = "default",
    kiro_cli: str = DEFAULT_KIRO_CLI,
    cwd: str = DEFAULT_KIRO_CWD,
    home: str = DEFAULT_HOME,
    proxy: str = DEFAULT_PROXY,
    api_key: str | None = None,
) -> LaunchSpec:
    """The argv, cwd and complete environment of the V3 ACP server process.

    ``api_key`` is put into ``KIRO_API_KEY`` only for the default auth method (option a); with
    ``--auth-method=client`` the client answers ``_kiro/auth/getAccessToken`` instead (option b).
    """
    if auth_method not in AUTH_METHODS:
        raise ValueError(f"auth_method must be one of {AUTH_METHODS}")
    if not kiro_cli.startswith("/"):
        raise ValueError("kiro_cli must be an absolute path")
    argv = [kiro_cli, "acp", "--agent-engine=v3"]
    if auth_method == "client":
        argv.append("--auth-method=client")
    assert not any(a == f or a.startswith(f + "=") for a in argv for f in V2_ONLY_FLAGS)
    env = {
        "HOME": home,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
    }
    if proxy:
        env.update({"HTTPS_PROXY": proxy, "https_proxy": proxy, "NO_PROXY": "localhost,127.0.0.1",
                    "no_proxy": "localhost,127.0.0.1"})  # fmt: skip
    if api_key and auth_method == "default":
        env["KIRO_API_KEY"] = api_key
    return LaunchSpec(argv=tuple(argv), cwd=cwd, env=env)


def initialize_params(client_name: str = "hermes-kiro-acp-v3", version: str = "0.2.0") -> dict[str, Any]:
    # No clientCapabilities: no fs/* or terminal/* bridge is offered to the agent.
    return {"protocolVersion": 1, "clientCapabilities": {}, "clientInfo": {"name": client_name, "version": version}}


def session_new_params(
    *, model_id: str | None, effort: str | None, mode_id: str = EXPECTED_MODE, cwd: str = DEFAULT_WORKSPACE
) -> dict[str, Any]:
    kiro: dict[str, str] = {"modeId": mode_id}
    if model_id:
        kiro["modelId"] = model_id
    if effort:
        kiro["effortLevel"] = effort
    return {"cwd": cwd, "mcpServers": [], "_meta": {"kiro": kiro}}


def session_load_params(session_id: str, *, cwd: str = DEFAULT_WORKSPACE) -> dict[str, Any]:
    # Client-owned inputs are sent again on load (the server does not persist them).
    return {"sessionId": session_id, "cwd": cwd, "mcpServers": []}


# ------------------------------------------------------------------------------- inspection
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def extension_methods(init_result: Mapping[str, Any]) -> frozenset[str]:
    caps = _dict(init_result.get("agentCapabilities"))
    methods = _dict(_dict(caps.get("_meta")).get("kiro")).get("extensionMethods")
    if not isinstance(methods, list):
        return frozenset()
    out: set[str] = set()
    for m in methods:
        if isinstance(m, str):
            out.add(m)
        elif isinstance(m, dict) and isinstance(m.get("name") or m.get("method"), str):
            out.add(str(m.get("name") or m.get("method")))
    return frozenset(out)


def prompt_capabilities(init_result: Mapping[str, Any]) -> dict[str, bool]:
    caps = _dict(_dict(init_result.get("agentCapabilities")).get("promptCapabilities"))
    return {k: v is True for k, v in caps.items()}


def config_option(result: Mapping[str, Any], config_id: str) -> dict[str, Any] | None:
    for opt in result.get("configOptions") or []:
        if isinstance(opt, dict) and config_id in (opt.get("id"), opt.get("configId")):
            return opt
    return None


def config_values(result: Mapping[str, Any], config_id: str) -> list[str]:
    opt = config_option(result, config_id)
    if opt is None:
        return []
    values: list[str] = []
    for entry in opt.get("options") or []:
        if isinstance(entry, dict):
            v = entry.get("value", entry.get("id"))
            if isinstance(v, str):
                values.append(v)
            for sub in entry.get("options") or []:  # grouped options
                if isinstance(sub, dict) and isinstance(sub.get("value"), str):
                    values.append(sub["value"])
        elif isinstance(entry, str):
            values.append(entry)
    return values


def config_current(result: Mapping[str, Any], config_id: str) -> str | None:
    opt = config_option(result, config_id)
    value = None if opt is None else opt.get("currentValue", opt.get("value"))
    return value if isinstance(value, str) else None


def current_mode(result: Mapping[str, Any]) -> str | None:
    """``modes.currentModeId`` (ACP), else the ``mode`` config option's current value."""
    mode = _dict(result.get("modes")).get("currentModeId")
    if isinstance(mode, str):
        return mode
    return config_current(result, "mode")


def _tool_names(entries: Any) -> list[str] | None:
    if not isinstance(entries, list):
        return None
    names: list[str] = []
    for e in entries:
        if isinstance(e, str):
            names.append(e)
        elif isinstance(e, dict):
            name = e.get("name", e.get("id", e.get("toolId")))
            names.append(name if isinstance(name, str) else json.dumps(e, sort_keys=True))
        else:
            names.append(repr(e))
    return names


def advertised_tools(payload: Mapping[str, Any]) -> list[str] | None:
    """The tool list a session result or a ``_kiro/tools/didChange`` notification advertises, if any."""
    for candidate in (
        payload.get("tools"),
        payload.get("availableTools"),
        _dict(_dict(payload.get("_meta")).get("kiro")).get("tools"),
    ):
        names = _tool_names(candidate)
        if names is not None:
            return names
    return None


def check_tools(tools: Iterable[str] | None, allowed: frozenset[str] = ALLOWED_TOOLS) -> None:
    if tools is None:
        return
    extra = sorted(set(tools) - allowed)
    if extra:
        raise SurfaceViolation("tools", ", ".join(extra)[:300])


def assert_surface(
    result: Mapping[str, Any], *, expected_mode: str = EXPECTED_MODE, allowed_tools: frozenset[str] = ALLOWED_TOOLS
) -> None:
    """Fail closed unless the session runs ``expected_mode`` and advertises no tool outside the allowlist.

    A missing mode counts as a mismatch. A missing tool list skips the tool half (the Hermes-side probe
    carries that load)."""
    mode = current_mode(result)
    if mode != expected_mode:
        raise SurfaceViolation("mode", f"current mode {mode!r}, expected {expected_mode!r}")
    check_tools(advertised_tools(result), allowed_tools)


def permission_response(params: Mapping[str, Any]) -> dict[str, Any]:
    """Reject, never allow: ``reject_once`` > ``reject_always`` > ``cancelled``; no consent metadata."""
    options = params.get("options")
    for kind in ("reject_once", "reject_always"):
        for opt in options if isinstance(options, list) else []:
            if isinstance(opt, dict) and opt.get("kind") == kind and isinstance(opt.get("optionId"), str):
                return {"outcome": {"outcome": "selected", "optionId": opt["optionId"]}}
    return {"outcome": {"outcome": "cancelled"}}


def pick_model(requested: str | None, advertised: Sequence[str], fallbacks: Sequence[str]) -> tuple[str | None, bool]:
    """``(model, fell_back)``: the requested id when advertised (or nothing is advertised), else the
    first advertised fallback, else ``None`` (keep the session default)."""
    if not requested or not advertised or requested in advertised:
        return requested, False
    for fb in fallbacks:
        if fb in advertised:
            return fb, True
    return None, True


# ------------------------------------------------------------------------------- transport
Handler = Callable[[dict[str, Any]], Any]


class Connection:
    """NDJSON JSON-RPC 2.0 over a byte stream pair, one reader thread, handlers registered up front."""

    def __init__(
        self,
        reader: IO[bytes],
        writer: IO[bytes],
        *,
        handlers: Mapping[str, Handler] | None = None,
        on_notification: Callable[[str, dict[str, Any]], None] | None = None,
        on_frame: Callable[[str, bytes], None] | None = None,
        logger: logging.Logger | None = None,
        max_line: int = MAX_LINE,
        max_malformed: int = MAX_MALFORMED,
    ) -> None:
        self._reader, self._writer = reader, writer
        self._handlers = dict(handlers or {})
        self._on_notification = on_notification
        self._on_frame = on_frame
        self._log = logger or log
        self._max_line, self._max_malformed = max_line, max_malformed
        self._ids = iter(range(1, 1 << 62))
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._redact_ids: set[Any] = set()
        self._malformed = 0
        self.closed = threading.Event()
        self.close_reason = ""
        self._thread = threading.Thread(target=self._read_loop, name="kiro-acp-v3-reader", daemon=True)

    def start(self) -> None:
        self._thread.start()

    # -------------------------------------------------------------- logging (never the token)
    def _log_frame(self, direction: str, frame: Mapping[str, Any]) -> None:
        if not self._log.isEnabledFor(logging.DEBUG):
            return
        shown = dict(frame)
        if direction == "out" and "result" in shown and shown.get("id") in self._redact_ids:
            shown["result"] = REDACTED
        if direction == "in" and shown.get("method") == TOKEN_METHOD:
            shown["params"] = REDACTED
        self._log.debug("acp %s %s", direction, json.dumps(shown, ensure_ascii=True)[:4000])

    # -------------------------------------------------------------- writing
    def _send(self, frame: dict[str, Any]) -> None:
        if self.closed.is_set():
            raise TransportClosed(self.close_reason or "closed")
        data = (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self._log_frame("out", frame)
        try:
            with self._write_lock:
                self._writer.write(data)
                self._writer.flush()
        except (OSError, ValueError) as exc:
            self._close(f"write failed: {exc}")
            raise TransportClosed(self.close_reason) from exc
        if self._on_frame is not None:
            self._on_frame("out", REDACTED.encode() if frame.get("id") in self._redact_ids else data)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(
        self, method: str, params: dict[str, Any], *, timeout: float, abort: Callable[[], bool] | None = None
    ) -> Any:
        """Send a request and wait for its response. ``abort`` is polled while waiting; when it returns
        True the wait ends with ``CancelTimeout`` (the response, if it still comes, is dropped)."""
        with self._lock:
            req_id = next(self._ids)
            box: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self._pending[req_id] = box
        try:
            self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            deadline = time.monotonic() + timeout
            while True:
                try:
                    msg = box.get(timeout=min(0.2, max(0.0, deadline - time.monotonic())) or 0.01)
                    break
                except queue.Empty:
                    if self.closed.is_set():
                        raise TransportClosed(self.close_reason or "closed") from None
                    if abort is not None and abort():
                        raise CancelTimeout(f"{method}: no response after session/cancel") from None
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out waiting for {method}") from None
        finally:
            with self._lock:
                self._pending.pop(req_id, None)
        if msg.get(_CLOSED):
            raise TransportClosed(self.close_reason or "closed")
        if "error" in msg:
            err = _dict(msg.get("error"))
            code = err.get("code")
            raise JsonRpcError(method, code if isinstance(code, int) else 0, str(err.get("message") or "error")[:500])
        return msg.get("result")

    def close(self, reason: str = "closed by client") -> None:
        self._close(reason)

    def _close(self, reason: str) -> None:
        if not self.closed.is_set():
            self.close_reason = reason
            self.closed.set()

    # -------------------------------------------------------------- reading
    def _readline(self) -> bytes | None:
        line = self._reader.readline(self._max_line + 1)
        if not line:
            return None
        if len(line) > self._max_line and not line.endswith(b"\n"):
            while True:  # discard the rest of the oversize line
                rest = self._reader.readline(65536)
                if not rest or rest.endswith(b"\n"):
                    break
            return b"\x00oversize"
        return line

    def _malformed_line(self, why: str) -> None:
        self._malformed += 1
        self._log.error("acp: malformed line from the agent (%s), %d of %d", why, self._malformed, self._max_malformed)
        if self._malformed >= self._max_malformed:
            self._close("malformed stream")

    def _read_loop(self) -> None:
        try:
            while not self.closed.is_set():
                line = self._readline()
                if line is None:
                    self._close("agent closed the stream")
                    break
                if line == b"\x00oversize":
                    self._malformed_line("line over the size limit")
                    continue
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    self._malformed_line("not JSON")
                    continue
                if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
                    self._malformed_line("not a JSON-RPC 2.0 object")
                    continue
                if self._on_frame is not None:
                    self._on_frame("in", line)
                self._log_frame("in", msg)
                self._dispatch(msg)
        except (OSError, ValueError) as exc:
            self._close(f"read failed: {exc}")
        finally:
            with self._lock:
                boxes = list(self._pending.values())
            for box in boxes:  # wake every waiter; request() then raises TransportClosed
                if box.empty():
                    box.put_nowait({_CLOSED: True})

    def _dispatch(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if isinstance(method, str) and "id" in msg:
            self._answer(msg["id"], method, _dict(msg.get("params")))
        elif isinstance(method, str):
            if self._on_notification is not None:
                try:
                    self._on_notification(method, _dict(msg.get("params")))
                except Exception:  # a sink bug must not kill the reader
                    self._log.exception("acp: notification handler failed for %s", method)
        elif "id" in msg and ("result" in msg or "error" in msg):
            with self._lock:
                box = self._pending.get(msg["id"]) if isinstance(msg["id"], int) else None
            if box is not None and box.empty():
                box.put_nowait(msg)
        else:
            self._malformed_line("neither request, notification nor response")

    def _answer(self, req_id: Any, method: str, params: dict[str, Any]) -> None:
        handler = self._handlers.get(method)
        if method == TOKEN_METHOD:
            self._redact_ids.add(req_id)
        if handler is None:
            error = {"code": METHOD_NOT_FOUND, "message": f"method not found: {method}"}
            frame: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "error": error}
        else:
            try:
                frame = {"jsonrpc": "2.0", "id": req_id, "result": handler(params)}
            except JsonRpcError as exc:
                frame = {"jsonrpc": "2.0", "id": req_id, "error": {"code": exc.code, "message": exc.message}}
            except Exception as exc:
                self._log.warning("acp: handler for %s failed: %s", method, type(exc).__name__)
                frame = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": "internal error"}}
        try:
            self._send(frame)
        except TransportClosed:
            pass
        finally:
            if method == TOKEN_METHOD:
                self._redact_ids.discard(req_id)


# ---------------------------------------------------------------------------------- client
@dataclass
class Session:
    id: str
    result: dict[str, Any]
    model: str | None = None
    effort: str | None = None
    tools: list[str] | None = None
    violation: SurfaceViolation | None = None
    config_sets: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class TurnResult:
    text: str
    reasoning: str
    stop_reason: str | None
    tool_calls: list[dict[str, Any]]


class _Sink:
    def __init__(self, on_update: Callable[[dict[str, Any]], None] | None) -> None:
        self.text: list[str] = []
        self.reasoning: list[str] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.on_update = on_update

    def feed(self, update: dict[str, Any]) -> None:
        kind = update.get("sessionUpdate")
        content = _dict(update.get("content"))
        text = content.get("text") if content.get("type", "text") == "text" else None
        if kind == "agent_message_chunk" and isinstance(text, str):
            self.text.append(text)
        elif kind == "agent_thought_chunk" and isinstance(text, str):
            self.reasoning.append(text)
        elif kind in ("tool_call", "tool_call_update"):
            self.tool_calls.append(update)
        if self.on_update is not None:
            self.on_update(update)


Process = subprocess.Popen  # type: ignore[type-arg]
Violation = Callable[[SurfaceViolation], None]


class KiroV3Agent:
    """One ``kiro-cli acp --agent-engine=v3`` process; sessions are created, checked and prompted on it."""

    def __init__(
        self,
        spawn: Callable[[], Any],
        *,
        auth_method: str = "default",
        token: Callable[[], str] | None = None,
        token_result: Callable[[str], dict[str, Any]] | None = None,
        expected_mode: str = EXPECTED_MODE,
        allowed_tools: frozenset[str] = ALLOWED_TOOLS,
        on_surface_violation: Violation | None = None,
        on_frame: Callable[[str, bytes], None] | None = None,
        logger: logging.Logger | None = None,
        cwd: str = DEFAULT_WORKSPACE,
    ) -> None:
        if auth_method not in AUTH_METHODS:
            raise ValueError(f"auth_method must be one of {AUTH_METHODS}")
        if auth_method == "client" and token is None:
            raise ValueError("client auth needs a token provider")
        self._spawn, self.auth_method, self._token = spawn, auth_method, token
        self._token_result = token_result or (lambda t: {"accessToken": t})
        self.expected_mode, self.allowed_tools = expected_mode, allowed_tools
        self._on_violation = on_surface_violation
        self._on_frame = on_frame
        self._log = logger or log
        self.cwd = cwd
        self.proc: Any = None
        self.conn: Connection | None = None
        self.init_result: dict[str, Any] = {}
        self._sinks: dict[str, _Sink] = {}
        self._sessions: dict[str, Session] = {}
        # session id -> monotonic deadline for the prompt response after session/cancel (inf: no bound)
        self._cancelled: dict[str, float] = {}
        self._lock = threading.RLock()

    # -------------------------------------------------------------- lifecycle
    @property
    def alive(self) -> bool:
        return (
            self.conn is not None and not self.conn.closed.is_set() and (self.proc is None or self.proc.poll() is None)
        )

    def start(self, *, timeout: float = 60.0) -> dict[str, Any]:
        with self._lock:
            self.close()
            self.proc = self._spawn()
            handlers: dict[str, Handler] = {"session/request_permission": self._permission}
            if self.auth_method == "client":
                handlers[TOKEN_METHOD] = self._access_token
            # Handlers and the update sink exist before the first request is sent (V3 guide §2).
            self.conn = Connection(
                self.proc.stdout,
                self.proc.stdin,
                handlers=handlers,
                on_notification=self._notification,
                on_frame=self._on_frame,
                logger=self._log,
            )
            self.conn.start()
            result = self.conn.request("initialize", initialize_params(), timeout=timeout)
            self.init_result = _dict(result)
            return self.init_result

    def close(self) -> None:
        with self._lock:
            if self.conn is not None:
                self.conn.close()
            proc, self.proc, self.conn = self.proc, None, None
            self._sinks.clear()
            self._sessions.clear()
            self._cancelled.clear()
        if proc is not None:
            stdin = getattr(proc, "stdin", None)
            with contextlib.suppress(OSError):
                if stdin is not None:
                    stdin.close()
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                with contextlib.suppress(Exception):
                    proc.kill()

    @property
    def extension_methods(self) -> frozenset[str]:
        return extension_methods(self.init_result)

    @property
    def prompt_capabilities(self) -> dict[str, bool]:
        return prompt_capabilities(self.init_result)

    def _conn(self) -> Connection:
        if self.conn is None:
            raise TransportClosed("not started")
        return self.conn

    # -------------------------------------------------------------- agent -> client
    def _permission(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = params.get("sessionId")
        if isinstance(sid, str) and sid in self._cancelled:
            # ACP prompt-turn cancellation: pending permission requests MUST be answered ``cancelled``.
            response: dict[str, Any] = {"outcome": {"outcome": "cancelled"}}
        else:
            response = permission_response(params)
        call = _dict(params.get("toolCall"))
        consent = _dict(_dict(_dict(params.get("_meta")).get("kiro")).get("consent"))
        self._log.info(
            "acp permission tool_call=%s capability=%s resource=%s decision=%s",
            str(call.get("toolCallId"))[:80],
            str(consent.get("capability"))[:40],
            str(consent.get("resource"))[:200],
            response["outcome"].get("optionId", "cancelled"),
        )
        return response

    def _access_token(self, params: dict[str, Any]) -> dict[str, Any]:
        assert self._token is not None
        return self._token_result(self._token())

    def _notification(self, method: str, params: dict[str, Any]) -> None:
        sid = params.get("sessionId")
        session = self._sessions.get(sid) if isinstance(sid, str) else None
        if method == "session/update":
            update = _dict(params.get("update"))
            if session is not None and update.get("sessionUpdate") == "current_mode_update":
                mode = update.get("currentModeId", update.get("modeId"))
                if mode != self.expected_mode:
                    session.violation = SurfaceViolation("mode", f"mode changed to {mode!r}")
            sink = self._sinks.get(sid) if isinstance(sid, str) else None
            if sink is not None:
                sink.feed(update)
        elif method == TOOLS_CHANGED:
            tools = advertised_tools(params)
            targets = [session] if session is not None else list(self._sessions.values())
            for s in targets:
                s.tools = tools
                try:
                    check_tools(tools, self.allowed_tools)
                except SurfaceViolation as exc:
                    s.violation = exc

    # -------------------------------------------------------------- sessions
    def _violation(self, exc: SurfaceViolation) -> SurfaceViolation:
        self._log.error("acp surface violation: %s: %s", exc.reason, exc.detail)
        if self._on_violation is not None:
            try:
                self._on_violation(exc)
            except Exception:
                self._log.exception("acp: surface violation hook failed")
        self.close()
        return exc

    def _check(self, session: Session) -> None:
        try:
            assert_surface(session.result, expected_mode=self.expected_mode, allowed_tools=self.allowed_tools)
            if session.violation is not None:
                raise session.violation
        except SurfaceViolation as exc:
            raise self._violation(exc) from None

    def new_session(
        self, *, model_id: str | None, effort: str | None, fallbacks: Sequence[str] = (), timeout: float = 60.0
    ) -> Session:
        conn = self._conn()
        params = session_new_params(model_id=model_id, effort=effort, mode_id=self.expected_mode, cwd=self.cwd)
        result = _dict(conn.request("session/new", params, timeout=timeout))
        sid = result.get("sessionId")
        if not isinstance(sid, str) or not sid:
            raise AcpError("session/new returned no sessionId")
        session = Session(id=sid, result=result, tools=advertised_tools(result))
        self._sessions[sid] = session
        self._check(session)
        self._apply_config(session, model_id, effort, fallbacks, timeout)
        return session

    def load_session(self, session_id: str, *, timeout: float = 60.0) -> Session:
        conn = self._conn()
        session = Session(id=session_id, result={})
        self._sessions[session_id] = session  # registered before the call: replay arrives while loading
        session.result = _dict(
            conn.request("session/load", session_load_params(session_id, cwd=self.cwd), timeout=timeout)
        )
        session.tools = advertised_tools(session.result) or session.tools
        self._check(session)
        return session

    def _apply_config(
        self, session: Session, model_id: str | None, effort: str | None, fallbacks: Sequence[str], timeout: float
    ) -> None:
        """Make the session's model and effort match the resolved values with ``session/set_config_option``
        (never ``session/set_model``), and only for options the session advertises."""
        advertised = config_values(session.result, "model")
        model, fell_back = pick_model(model_id, advertised, fallbacks)
        if fell_back:
            self._log.warning("acp: model %r is not offered by this session; using %r", model_id, model or "default")
        current = config_current(session.result, "model")
        if model and config_option(session.result, "model") is not None and current != model:
            self._set_option(session, "model", model, timeout)
        session.model = model or current
        current_effort = config_current(session.result, "effortLevel")
        efforts = config_values(session.result, "effortLevel")
        advertised_effort = config_option(session.result, "effortLevel") is not None
        if effort and advertised_effort and current_effort != effort and (not efforts or effort in efforts):
            self._set_option(session, "effortLevel", effort, timeout)
            current_effort = effort
        session.effort = current_effort or effort

    def _set_option(self, session: Session, config_id: str, value: str, timeout: float) -> None:
        result = self._conn().request(
            "session/set_config_option",
            {"sessionId": session.id, "configId": config_id, "value": value},
            timeout=timeout,
        )
        session.config_sets.append((config_id, value))
        updated = result.get("configOptions") if isinstance(result, dict) else None
        merged = {str(o.get("id")): o for o in session.result.get("configOptions") or [] if isinstance(o, dict)}
        for opt in updated if isinstance(updated, list) else []:  # replace advertised entries by id
            if isinstance(opt, dict) and isinstance(opt.get("id"), str):
                merged[opt["id"]] = opt
        if config_id in merged:
            merged[config_id] = {**merged[config_id], "currentValue": value}
        session.result = {**session.result, "configOptions": list(merged.values())}

    def prompt(
        self,
        session: Session,
        blocks: list[dict[str, Any]],
        *,
        timeout: float = 900.0,
        on_update: Callable[[dict[str, Any]], None] | None = None,
    ) -> TurnResult:
        """One turn; it completes only on the ``session/prompt`` response (never on a turn-end update)."""
        self._check(session)
        conn = self._conn()
        sink = _Sink(on_update)
        self._sinks[session.id] = sink

        def overdue() -> bool:  # a cancel was sent and its grace period ran out
            return time.monotonic() >= self._cancelled.get(session.id, math.inf)

        try:
            result = conn.request(
                "session/prompt", {"sessionId": session.id, "prompt": blocks}, timeout=timeout, abort=overdue
            )
        except TimeoutError:
            self.cancel(session)
            raise
        finally:
            self._sinks.pop(session.id, None)
        if session.violation is not None:
            raise self._violation(session.violation)
        stop = _dict(result).get("stopReason")
        return TurnResult(
            "".join(sink.text), "".join(sink.reasoning), stop if isinstance(stop, str) else None, sink.tool_calls
        )

    def cancel(self, session: Session, *, grace_s: float | None = None) -> None:
        """Send ``session/cancel`` (a notification; safe from any thread). Updates keep being read and the
        turn still ends on the ``session/prompt`` response (``stopReason: "cancelled"``); later permission
        requests of the session are answered ``cancelled``. With ``grace_s`` the waiting ``prompt`` gives
        up after that many seconds with ``CancelTimeout``."""
        deadline = math.inf if grace_s is None else time.monotonic() + grace_s
        with self._lock:
            self._cancelled[session.id] = min(deadline, self._cancelled.get(session.id, math.inf))
        with contextlib.suppress(TransportClosed):
            self._conn().notify("session/cancel", {"sessionId": session.id})

    def forget(self, session: Session) -> None:
        self._sessions.pop(session.id, None)
        self._sinks.pop(session.id, None)
        self._cancelled.pop(session.id, None)


def popen_spawn(spec: LaunchSpec, *, user: str | None = None) -> Callable[[], Any]:
    """A spawn callable for ``KiroV3Agent``; ``user`` wraps the argv in ``runuser -u <user> --`` (root).

    The environment (which may hold ``KIRO_API_KEY``) is passed as the process environment, never on
    the command line, where any local user could read it."""
    argv = list(spec.argv)
    if user is not None:
        argv = ["/usr/sbin/runuser", "-u", user, "--", *argv]

    def spawn() -> Any:
        return subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=spec.cwd,
            env=dict(spec.env),
            close_fds=True,
        )

    return spawn
