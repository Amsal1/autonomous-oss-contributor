"""Hermes provider glue for the Kiro CLI V3 ACP server (``HERMES_KIRO_ACP_ENGINE=v3``, the default).

Hermes drives model providers through an OpenAI-client facade (``client.chat.completions.create``)
and owns the conversation: every completion carries the whole transcript and Hermes' own tool
definitions, rendered into one text prompt with the in-tree ACP bridge helpers (``<tool_call>`` blocks
are parsed back out of the answer). This client keeps one ``kiro-cli acp --agent-engine=v3`` process
and opens a fresh, surface-checked session per completion:

* the model and effort come from a models file read live (mtime-cached), so a model switch applies to
  the next completion without a restart: the values go into ``session/new`` ``_meta.kiro`` and, when
  the session still reports another model, one ``session/set_config_option`` follows (never
  ``session/set_model``);
* image parts of the latest user message become ACP image blocks when the server advertises
  ``promptCapabilities.image``; such a turn uses the vision role;
* a dropped transport is reconnected and the session loaded again, but the prompt is never resent;
* a surface violation (wrong mode, a tool outside the allowlist) fails the turn and runs the configured
  hook (ossbot: a soft kill over the gatekeeper's Unix socket).

Configuration is by environment (the ossbot unit sets it): see ``V3Settings.from_env``.
"""

from __future__ import annotations

import base64
import binascii
import http.client
import json
import logging
import os
import re
import socket
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from . import v3_core as core

log = logging.getLogger("kiro_acp.v3")

DEFAULT_TIMEOUT_S = 900.0
MAX_IMAGE_BYTES = 20 * 1024 * 1024
_DATA_URL = re.compile(r"^data:(image/(?:png|jpeg|jpg|webp|gif));base64,([A-Za-z0-9+/=\s]+)$", re.DOTALL)
_PREAMBLE = ("You are the language model behind the Hermes assistant, reached over ACP.",)
# only when Hermes offered tools in this completion (an auxiliary call such as a title offers none)
_TOOL_PREAMBLE = (
    "The Hermes tools listed below are not in your own tool list, but they are real and available now: Hermes "
    "runs them on the host when your answer contains a call block, and the result comes back in the next message.",
    'To use one, output only <tool_call>{"id": "<new unique id>", "type": "function", "function": {"name": '
    '"<tool name>", "arguments": {<the arguments as a JSON object>}}}</tool_call> blocks for that step and stop'
    ' there; never write a tool result yourself. Escape quotes and newlines inside JSON strings (\\" and \\n).',
)
# Hermes' own contract asks for arguments as a JSON *string*; double-encoded program text is where models
# break the JSON, so the provider asks for an object (both parsers accept one).
_HERMES_STRING_ARGS = "arguments must be a JSON string."
_OBJECT_ARGS = "arguments is a JSON object."
_PREAMBLE_TAIL = (
    "You may use your own web_search and web_fetch tools for current information; cite the URLs you used.",
    "If no tool is needed, answer normally.",
)
_ROLE_LABELS = {"system": "System", "user": "User", "assistant": "Assistant", "tool": "Tool"}


class TurnLost(core.AcpError):
    """The transport dropped during a turn; the session was reloaded and the prompt was not resent."""


# --------------------------------------------------------------------------------- settings
@dataclass(frozen=True)
class V3Settings:
    kiro_cli: str = core.DEFAULT_KIRO_CLI
    auth_method: str = "default"
    token_file: str | None = None
    kiro_cwd: str = core.DEFAULT_KIRO_CWD
    workspace: str = core.DEFAULT_WORKSPACE
    home: str = core.DEFAULT_HOME
    proxy: str = core.DEFAULT_PROXY
    models_file: str | None = None
    chat_role: str = "chat"
    vision_role: str = "vision"
    # Roles a request may select with ``model="role:<name>"`` (e.g. a live check on a cheap test model);
    # empty = the request's model is always ignored. ``<name>_vision`` is used for image turns when listed.
    request_roles: frozenset[str] = frozenset()
    expected_mode: str = core.EXPECTED_MODE
    allowed_tools: frozenset[str] = core.ALLOWED_TOOLS
    violation_uds: str | None = None
    violation_reason: str = "hermes_kiro_surface"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> V3Settings:
        e = os.environ if env is None else env

        def get(name: str, default: str) -> str:
            return (e.get(f"HERMES_KIRO_ACP_{name}") or "").strip() or default

        tools = get("ALLOWED_TOOLS", ",".join(sorted(core.ALLOWED_TOOLS)))
        return cls(
            kiro_cli=get("CLI", core.DEFAULT_KIRO_CLI),
            auth_method=get("AUTH", "default"),
            token_file=get("TOKEN_FILE", "") or None,
            kiro_cwd=get("CWD", core.DEFAULT_KIRO_CWD),
            workspace=get("WORKSPACE", core.DEFAULT_WORKSPACE),
            home=get("HOME", core.DEFAULT_HOME),
            proxy=get("PROXY", core.DEFAULT_PROXY),
            models_file=get("MODELS_FILE", "") or None,
            chat_role=get("CHAT_ROLE", "chat"),
            vision_role=get("VISION_ROLE", "vision"),
            request_roles=frozenset(r.strip() for r in get("REQUEST_ROLES", "").split(",") if r.strip()),
            expected_mode=get("EXPECTED_MODE", core.EXPECTED_MODE),
            allowed_tools=frozenset(t.strip() for t in tools.split(",") if t.strip()),
            violation_uds=get("VIOLATION_UDS", "") or None,
        )


def read_env_value(path: str, key: str) -> str:
    """``key`` from a ``KEY=VALUE`` file (the token for client auth); the value is never logged."""
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            name, sep, value = line.strip().partition("=")
            if sep and name.strip() == key:
                return value.strip().strip("'\"")
    raise core.AcpError(f"{key} not found in the token file")


# ----------------------------------------------------------------------------------- models
@dataclass(frozen=True)
class ResolvedModel:
    model: str | None
    effort: str | None
    fallbacks: tuple[str, ...] = ()


class ModelsSource:
    """mtime-cached reader of a models file: ``default.{model,effort}``, ``roles.<role>.{model,effort}``
    (a model naming ``default`` or another role inherits it) and ``fallbacks`` (a list of ids)."""

    def __init__(self, path: str | None, *, load: Callable[[str], Any] | None = None) -> None:
        self.path = path
        self._load = load or _load_yaml
        self._stamp: tuple[int, int] | None = None
        self._data: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _current(self) -> dict[str, Any]:
        if not self.path:
            return {}
        with self._lock:
            try:
                st = os.stat(self.path)
            except OSError as exc:
                log.warning("models file unreadable (%s); keeping the last good copy", exc.strerror)
                return self._data
            stamp = (st.st_mtime_ns, st.st_size)
            if stamp != self._stamp:
                try:
                    with open(self.path, encoding="utf-8") as fh:
                        data = self._load(fh.read())
                except Exception as exc:
                    log.warning("models file invalid (%s); keeping the last good copy", type(exc).__name__)
                    return self._data
                if isinstance(data, dict):
                    self._data, self._stamp = data, stamp
            return self._data

    def resolve(self, role: str) -> ResolvedModel:
        data = self._current()
        roles = _mapping(data.get("roles"))
        default = _mapping(data.get("default"))
        fallbacks = tuple(f for f in data.get("fallbacks") or [] if isinstance(f, str))
        name, effort, seen = role, None, set()
        for _ in range(8):
            entry = _mapping(roles.get(name))
            if not entry:
                break
            seen.add(name)
            effort = effort or _str(entry.get("effort"))
            model = _str(entry.get("model"))
            if model is None:
                break
            if model == "default":
                return ResolvedModel(_str(default.get("model")), effort or _str(default.get("effort")), fallbacks)
            if model in roles and model not in seen:
                name = model
                continue
            return ResolvedModel(model, effort, fallbacks)
        return ResolvedModel(_str(default.get("model")), effort or _str(default.get("effort")), fallbacks)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _load_yaml(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        pass
    # Hermes v0.22.2 ships ruamel.yaml, not PyYAML (found live in FEAT-007 item 38); either one will do.
    try:
        from ruamel.yaml import YAML
    except ImportError:
        import yaml

        return yaml.safe_load(text)
    return YAML(typ="safe", pure=True).load(text)


# ---------------------------------------------------------------------------- violation hook
class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


def uds_kill_hook(path: str, reason: str = "hermes_kiro_surface", timeout: float = 5.0) -> core.Violation:
    """``POST /v1/control/kill {reason}`` over a Unix socket (ossbot: the gatekeeper; requester role)."""

    def hook(exc: core.SurfaceViolation) -> None:
        conn = _UnixHTTPConnection(path, timeout)
        try:
            body = json.dumps({"reason": reason})
            conn.request("POST", "/v1/control/kill", body=body, headers={"Content-Type": "application/json"})
            status = conn.getresponse().status
            log.error("acp surface violation (%s); kill requested, status %s", exc.reason, status)
        finally:
            conn.close()

    return hook


# --------------------------------------------------------------------------------- messages
def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        text = content.get("text")
        return text.strip() if isinstance(text, str) else ""
    if isinstance(content, list):
        return "\n".join(t for t in (_content_text(p) for p in content if not _is_image_part(p)) if t).strip()
    return str(content).strip()


def _is_image_part(part: Any) -> bool:
    return isinstance(part, dict) and part.get("type") in ("image_url", "input_image")


def latest_images(messages: Sequence[Any]) -> list[tuple[str, str]]:
    """``(mime, base64)`` of the ``data:image/...`` parts in the latest user message (remote URLs are
    not fetched by the provider)."""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            out: list[tuple[str, str]] = []
            content = message.get("content")
            for part in content if isinstance(content, list) else []:
                if not _is_image_part(part):
                    continue
                url = part.get("image_url")
                url = url.get("url") if isinstance(url, dict) else url
                m = _DATA_URL.match(url) if isinstance(url, str) else None
                if m is None:
                    continue
                data = re.sub(r"\s+", "", m.group(2))
                try:
                    raw = base64.b64decode(data, validate=True)
                except (binascii.Error, ValueError):
                    continue
                if 0 < len(raw) <= MAX_IMAGE_BYTES:
                    out.append(("image/jpeg" if m.group(1) == "image/jpg" else m.group(1), data))
            return out
    return []


def format_prompt(messages: Sequence[Any], tools: Any, tool_choice: Any, bridge: Any) -> str:
    offered = tool_names(tools) if bridge is not None else []
    bridged = bridge.render_tool_bridge_sections(tools, tool_choice) if bridge is not None else []
    sections = [*_PREAMBLE, *(_TOOL_PREAMBLE if offered else ()), *_PREAMBLE_TAIL,
                *(str(s).replace(_HERMES_STRING_ARGS, _OBJECT_ARGS) for s in bridged)]  # fmt: skip
    transcript: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "context").strip().lower()
        text = _content_text(message.get("content"))
        if role == "assistant" and message.get("tool_calls"):
            text = (text + "\n" + json.dumps(message["tool_calls"], ensure_ascii=False)).strip()
        if text:
            transcript.append(f"{_ROLE_LABELS.get(role, 'Context')}:\n{text}")
    if transcript:
        sections.append("Conversation transcript:\n\n" + "\n\n".join(transcript))
    if offered:  # recency: the tool list is far above a long transcript
        sections.append("Hermes tools you can call now with a <tool_call> block: " + ", ".join(offered) + ".")
    sections.append("Continue the conversation from the latest user request.")
    return "\n\n".join(s.strip() for s in sections if s and str(s).strip())


def _hermes_bridge() -> Any:
    from agent import acp_openai_bridge  # Hermes in-tree helpers (text rendering only)

    return acp_openai_bridge


def tool_names(tools: Any) -> list[str]:
    """Names of the OpenAI-shape tool definitions Hermes offered (malformed entries are skipped)."""
    names: list[str] = []
    for tool in tools if isinstance(tools, list) else []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        name = fn.get("name") if isinstance(fn, dict) else None
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


_TOOL_CALL_OPEN = re.compile(r"<tool_call>\s*", re.IGNORECASE)


def _tool_call(bridge: Any, call_id: str, name: str, arguments: str) -> Any:
    build = getattr(bridge, "build_openai_tool_call", None)
    if callable(build):
        return build(call_id=call_id, name=name, arguments=arguments)
    return SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(name=name, arguments=arguments))


def _lenient_call(obj: Any, offered: set[str]) -> tuple[str, str, str | None] | str:
    """``(name, arguments json, id)`` from the shapes models write besides the OpenAI one:
    ``{"name", "arguments"|"parameters"|"input"}`` or ``{"function": {...}}`` with object arguments,
    else the reason it was refused (``shape``, ``unoffered``, ``args``). Only a tool Hermes offered in
    this completion is accepted."""
    if not isinstance(obj, dict):
        return "shape"
    inner = obj.get("function")
    fn: dict[str, Any] = inner if isinstance(inner, dict) else obj
    name = fn.get("name")
    if not isinstance(name, str) or not name.strip():
        return "shape"
    if name.strip() not in offered:
        return "unoffered"
    args: Any = {}
    for key in ("arguments", "parameters", "input", "args"):
        if key in fn:
            args = fn[key]
            break
    if isinstance(args, str):
        try:
            args = json.loads(args, strict=False) if args.strip() else {}
        except ValueError:
            return "args"
    if not isinstance(args, dict):
        return "args"
    call_id = obj.get("id")
    return name.strip(), json.dumps(args, ensure_ascii=False), call_id if isinstance(call_id, str) and call_id else None


@dataclass
class ToolExtraction:
    calls: list[Any]
    cleaned: str
    blocks: int  # ``<tool_call>`` openings in the answer
    rejected: dict[str, int] = field(default_factory=dict)  # lenient-pass refusals by reason (metadata only)


def extract_tool_calls(bridge: Any, text: str, offered: Sequence[str]) -> ToolExtraction:
    """Hermes' parser first; when it found nothing although the answer holds ``<tool_call>`` blocks, a
    lenient pass for the other common shapes (offered tools only). When calls were found in blocks, the
    message text is what came before the first block: models sometimes go on to invent the tool results."""
    text = text or ""
    first = _TOOL_CALL_OPEN.search(text)
    blocks = len(_TOOL_CALL_OPEN.findall(text))
    if bridge is None:
        return ToolExtraction([], text, blocks)
    calls, cleaned = bridge.extract_tool_calls_from_text(text)
    calls = list(calls)
    rejected: dict[str, int] = {}
    if calls and not offered:  # e.g. a title request: no tool was offered, so no call can be honoured
        rejected["unoffered"] = len(calls)
        return ToolExtraction([], cleaned, blocks, rejected)
    if not calls and first is not None and offered:
        names = set(offered)
        decoder = json.JSONDecoder(strict=False)  # models put raw newlines of a program inside JSON strings
        for m in _TOOL_CALL_OPEN.finditer(text):
            try:
                obj, _end = decoder.raw_decode(text, m.end())
            except json.JSONDecodeError as exc:  # the decoder's own message only, never the text
                reason = f"json({exc.msg})"
                rejected[reason] = rejected.get(reason, 0) + 1
                continue
            parsed = _lenient_call(obj, names)
            if isinstance(parsed, str):
                rejected[parsed] = rejected.get(parsed, 0) + 1
                continue
            name, arguments, call_id = parsed
            calls.append(_tool_call(bridge, call_id or f"acp_call_{len(calls) + 1}", name, arguments))
    if calls and first is not None:
        cleaned = text[: first.start()].strip()
    return ToolExtraction(calls, cleaned, blocks, rejected)


class _Completion(SimpleNamespace):
    """A finished completion that is also awaitable: Hermes' auxiliary async path (vision, titles)
    awaits ``create()`` on ACP shims that opt out of its async wrapper (``HERMES_SKIP_ASYNC_WRAP``)."""

    def __await__(self) -> Any:
        return _resolved(self).__await__()


async def _resolved(value: Any) -> Any:
    return value


# ----------------------------------------------------------------------------------- client
@dataclass
class TurnInfo:
    """What the last completion did (for diagnostics and the ossbot selftest)."""

    session_id: str = ""
    role: str = ""
    model: str | None = None
    effort: str | None = None
    config_sets: list[tuple[str, str]] = field(default_factory=list)
    images: int = 0
    latency_s: float = 0.0
    stop_reason: str | None = None


class KiroV3Client:
    """OpenAI-client-compatible facade over one Kiro V3 ACP process."""

    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(
        self,
        *,
        settings: V3Settings | None = None,
        spawn: Callable[[], Any] | None = None,
        bridge: Any = None,
        models: ModelsSource | None = None,
        on_surface_violation: core.Violation | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        **_: Any,
    ) -> None:
        self.settings = settings or V3Settings.from_env()
        self.api_key, self.base_url = api_key or "kiro-acp", base_url or "acp://kiro"
        self._spawn = spawn
        self._bridge = bridge
        self.models = models or ModelsSource(self.settings.models_file)
        hook = on_surface_violation
        if hook is None and self.settings.violation_uds:
            hook = uds_kill_hook(self.settings.violation_uds, self.settings.violation_reason)
        self._hook = hook
        self._agent: core.KiroV3Agent | None = None
        self._active: core.Session | None = None
        self._lock = threading.Lock()  # one turn at a time on the process
        self._state = threading.Lock()  # agent/active swaps; close() never waits for a turn
        self.last_turn = TurnInfo()
        self.is_closed = False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create_chat_completion))

    # -------------------------------------------------------------- process
    def _spawner(self) -> Callable[[], Any]:
        if self._spawn is not None:
            return self._spawn
        s = self.settings
        key = os.environ.get("KIRO_API_KEY") if s.auth_method == "default" else None
        spec = core.launch_spec(
            auth_method=s.auth_method, kiro_cli=s.kiro_cli, cwd=s.kiro_cwd, home=s.home, proxy=s.proxy, api_key=key
        )
        return core.popen_spawn(spec)

    def _token(self) -> str:
        if not self.settings.token_file:
            raise core.AcpError("client auth needs HERMES_KIRO_ACP_TOKEN_FILE")
        return read_env_value(self.settings.token_file, "KIRO_API_KEY")

    def _new_agent(self) -> core.KiroV3Agent:
        s = self.settings
        agent = core.KiroV3Agent(
            self._spawner(),
            auth_method=s.auth_method,
            token=self._token if s.auth_method == "client" else None,
            expected_mode=s.expected_mode,
            allowed_tools=s.allowed_tools,
            on_surface_violation=self._hook,
            cwd=s.workspace,
        )
        agent.start()
        return agent

    def _ensure_agent(self) -> core.KiroV3Agent:
        with self._state:
            self.is_closed = False
            agent = self._agent
        if agent is None or not agent.alive:
            if agent is not None:
                agent.close()
            agent = self._new_agent()
            with self._state:
                self._agent = agent
        return agent

    def close(self, *, grace_s: float = 2.0) -> None:
        """Cancel a running turn (``session/cancel``), give it ``grace_s`` to settle, then stop the process."""
        with self._state:
            self.is_closed = True
            agent, active = self._agent, self._active
        if agent is not None and active is not None:
            agent.cancel(active)
            deadline = time.monotonic() + grace_s
            while self._active is not None and time.monotonic() < deadline:
                time.sleep(0.05)
        with self._state:
            agent, self._agent = self._agent, None
        if agent is not None:
            agent.close()

    # -------------------------------------------------------------- completions
    def request_role(self, model: str | None, images: bool) -> str | None:
        """``role:<name>`` from the request when ``<name>`` is in ``request_roles``, else None."""
        if not isinstance(model, str) or not model.startswith("role:"):
            return None
        name = model[5:].strip()
        if name not in self.settings.request_roles:
            return None
        vision = f"{name}_vision"
        return vision if images and vision in self.settings.request_roles else name

    def _turn(
        self, text: str, images: list[tuple[str, str]], timeout: float, role: str | None = None
    ) -> core.TurnResult:
        role = role or (self.settings.vision_role if images else self.settings.chat_role)
        resolved = self.models.resolve(role)
        started = time.monotonic()
        with self._lock:
            agent = self._ensure_agent()
            blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
            if images and agent.prompt_capabilities.get("image"):
                blocks += [{"type": "image", "mimeType": mime, "data": data} for mime, data in images]
            elif images:
                blocks[0]["text"] += (
                    f"\n\n[The user attached {len(images)} image(s), but this model connection"
                    " cannot receive images. Say so instead of guessing their content.]"
                )
            session = agent.new_session(model_id=resolved.model, effort=resolved.effort, fallbacks=resolved.fallbacks)
            with self._state:
                self._active = session
            try:
                result = agent.prompt(session, blocks, timeout=timeout)
            except core.TransportClosed as exc:
                if self.is_closed:
                    raise core.AcpError("the turn was cancelled") from exc
                # V3 guide: reconnect with handlers registered, session/load, never resend the prompt.
                agent.close()
                agent = self._new_agent()
                with self._state:
                    self._agent = agent
                agent.load_session(session.id)
                raise TurnLost(
                    "the Kiro connection dropped during the turn; the answer was lost and the prompt was not resent"
                ) from exc
            finally:
                with self._state:
                    self._active = None
                agent.forget(session)
            self.last_turn = TurnInfo(
                session.id,
                role,
                session.model,
                session.effort,
                list(session.config_sets),
                len(images) if blocks[1:] else 0,
                time.monotonic() - started,
                result.stop_reason,
            )
            t = self.last_turn
            # metadata only (no prompt or answer text): which model a turn really ran on
            log.info("kiro_acp turn: role=%s model=%s effort=%s config_sets=%d images=%d stop=%s %.1fs",
                     t.role, t.model, t.effort, len(t.config_sets), t.images, t.stop_reason, t.latency_s)  # fmt: skip
            return result

    def _create_chat_completion(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        timeout: Any = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        stream: bool = False,
        **_: Any,
    ) -> Any:
        # The models file decides; Hermes' provider name is not a Kiro model id. Only ``role:<name>`` of an
        # allowlisted request role picks another role of the same file.
        bridge = self._bridge if self._bridge is not None else _hermes_bridge()
        msgs = list(messages or [])
        offered = tool_names(tools)
        text = format_prompt(msgs, tools, tool_choice, bridge)
        images = latest_images(msgs)
        result = self._turn(text, images, _timeout_s(timeout), self.request_role(model, bool(images)))
        found = extract_tool_calls(bridge, result.text, offered)
        tool_calls, cleaned = found.calls, found.cleaned
        # metadata only: how many Hermes tools went into the prompt and how many calls came back out
        rejected = ",".join(f"{k}:{v}" for k, v in sorted(found.rejected.items())) or "-"
        log.info("kiro_acp tools: offered=%d bridged=%s blocks=%d parsed=%d rejected=%s names=%s",
                 len(offered), bridge is not None and bool(offered), found.blocks, len(tool_calls), rejected,
                 ",".join(sorted(offered))[:400])  # fmt: skip
        message = SimpleNamespace(
            content=cleaned,
            tool_calls=tool_calls,
            reasoning=result.reasoning or None,
            reasoning_content=result.reasoning or None,
            reasoning_details=None,
        )
        completion = _Completion(
            choices=[SimpleNamespace(message=message, finish_reason="tool_calls" if tool_calls else "stop")],
            usage=SimpleNamespace(
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            ),
            model=self.last_turn.model or "kiro-acp",
        )
        if stream and bridge is not None:
            return bridge.completion_to_stream_chunks(completion)
        return completion


def _timeout_s(timeout: Any) -> float:
    if isinstance(timeout, (int, float)) and timeout > 0:
        return float(timeout)
    values = [getattr(timeout, a, None) for a in ("read", "write", "connect", "pool", "timeout")]
    nums = [float(v) for v in values if isinstance(v, (int, float)) and v > 0]
    return max(nums) if nums else DEFAULT_TIMEOUT_S


def list_models(
    settings: V3Settings | None = None, *, spawn: Callable[[], Any] | None = None, timeout: float = 30.0
) -> list[str] | None:
    """Model ids advertised by a short-lived V3 session (no prompt); ``None`` when unavailable."""
    client = KiroV3Client(settings=settings, spawn=spawn, on_surface_violation=lambda exc: None)
    try:
        agent = client._ensure_agent()
        session = agent.new_session(model_id=None, effort=None, timeout=timeout)
        return core.config_values(session.result, "model") or None
    except Exception:
        return None
    finally:
        client.close()
