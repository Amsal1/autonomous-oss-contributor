"""ossbot tools for Hermes (DESIGN §15.1): read status and file requests over the gatekeeper UDS.

The gatekeeper derives the caller's role from the socket peer uid (``hermes`` = requester), so these
tools can read status, file a discovery request for an issue URL, send the owner a note, pause or
kill, and (tier ``self_improve``) run a short program in a ``--network none`` sandbox job
(``ossbot_request_sandbox_job``); they cannot approve, submit, resume or change settings.
``ossbot_feature_*`` work on the
per-feature SQLite stores (``features.py``) without the gatekeeper. Stdlib only; never imports ossbot.
Every result is a JSON string; gatekeeper errors are returned as ``{"success": false, ...}``.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import sqlite3
import time
import urllib.parse
from typing import Any

from .features import FEATURES, FeatureStore

SOCKET = os.environ.get("OSSBOT_GATEKEEPER_SOCK", "/run/ossbot/gatekeeper.sock")
CID_RE = re.compile(r"^c_[0-9A-Z]{10}$")
STAGE_RE = re.compile(r"^[a-z]{1,16}$")
MAX_BODY = 256 * 1024


class _UDS(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("ossbot", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


def session_key() -> str:
    """The session key of the turn this tool call serves (the gatekeeper's ``X-Hermes-Session-Key``),
    from Hermes's per-turn session vars (``gateway.session_context``), else the environment."""
    try:
        from gateway.session_context import get_session_env  # type: ignore[import-not-found]  # noqa: PLC0415 - Hermes-side only

        return str(get_session_env("HERMES_SESSION_KEY", "") or "")[:256]
    except ImportError:
        return os.environ.get("HERMES_SESSION_KEY", "")[:256]


def call(method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float = 20.0,
         session: bool = False) -> tuple[int, Any]:  # fmt: skip
    """``session``: send the turn's session key (U12: unsolicited messages attach to that turn only)."""
    conn = _UDS(SOCKET, timeout)
    try:
        payload = None if body is None else json.dumps(body)
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        key = session_key() if session else ""
        if key and not any(c in key for c in "\r\n\x00"):
            headers["X-Hermes-Session-Key"] = key
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read(MAX_BODY + 1)[:MAX_BODY]
        try:
            return resp.status, json.loads(raw) if raw else None
        except ValueError:
            return resp.status, None
    finally:
        conn.close()


def _result(status: int, data: Any, ok: tuple[int, ...] = (200, 201, 202)) -> str:
    if status in ok:
        return json.dumps({"success": True, "data": data}, ensure_ascii=False)
    return json.dumps({"success": False, "status": status, "error": data}, ensure_ascii=False)


def _safe(fn: Any) -> Any:
    def wrapper(params: dict[str, Any], **_: Any) -> str:
        try:
            return str(fn(params if isinstance(params, dict) else {}))
        except (OSError, http.client.HTTPException) as exc:
            return json.dumps({"success": False, "error": f"gatekeeper unavailable: {type(exc).__name__}"})
        except ValueError as exc:
            return json.dumps({"success": False, "error": str(exc)[:200]})
        except sqlite3.Error as exc:
            return json.dumps({"success": False, "error": f"feature store: {type(exc).__name__}"})

    wrapper.__name__ = fn.__name__
    return wrapper


def _text(params: dict[str, Any], key: str, limit: int) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    if len(value) > limit:
        raise ValueError(f"{key} is longer than {limit} characters")
    return value.strip()


@_safe
def ossbot_status(params: dict[str, Any]) -> str:
    return _result(*call("GET", "/v1/control"))


@_safe
def ossbot_queue(params: dict[str, Any]) -> str:
    return _result(*call("GET", "/v1/contributions?active=true&limit=50"))


@_safe
def ossbot_contribution(params: dict[str, Any]) -> str:
    cid = _text(params, "cid", 13)
    if not CID_RE.match(cid):
        raise ValueError("cid must look like c_XXXXXXXXXX")
    return _result(*call("GET", f"/v1/contributions/{cid}"))


@_safe
def ossbot_logs(params: dict[str, Any]) -> str:
    cid = _text(params, "cid", 13)
    if not CID_RE.match(cid):
        raise ValueError("cid must look like c_XXXXXXXXXX")
    stage = params.get("stage")
    if isinstance(stage, str) and stage and not STAGE_RE.match(stage):
        raise ValueError("stage must be a lowercase stage name")
    status, data = call("GET", "/v1/stage-runs?" + urllib.parse.urlencode({"cid": cid}))
    if status == 200 and isinstance(data, list) and isinstance(stage, str) and stage:
        data = [r for r in data if isinstance(r, dict) and r.get("stage") == stage]
    return _result(status, data)


@_safe
def ossbot_request_discovery(params: dict[str, Any]) -> str:
    return _result(*call("POST", "/v1/discovery-requests", {"url": _text(params, "url", 300)}))


@_safe
def ossbot_notify(params: dict[str, Any]) -> str:
    priority = params.get("priority", "P2")
    if priority not in ("P1", "P2"):
        raise ValueError('priority must be "P1" or "P2"')
    body = {"text": _text(params, "text", 4000), "priority": priority}
    return _result(*call("POST", "/v1/notify", body, session=True))


FEATURE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


@_safe
def ossbot_reminder_create(params: dict[str, Any]) -> str:
    """A reminder in this conversation (DESIGN §14.19 item 3); ``when`` is ISO 8601 with a zone."""
    body = {"when": _text(params, "when", 40), "text": _text(params, "text", 500)}
    return _result(*call("POST", "/v1/reminders", body, session=True))


@_safe
def ossbot_feature_card(params: dict[str, Any]) -> str:
    """Show a feature card in the conversation (DESIGN §14.12.7); the gatekeeper validates ``data``."""
    feature, kind = _text(params, "feature", 32), _text(params, "kind", 32)
    if not FEATURE_RE.match(feature) or not FEATURE_RE.match(kind):
        raise ValueError("feature and kind are lowercase names")
    data = params.get("data")
    if not isinstance(data, dict):
        raise ValueError("data must be a JSON object")
    return _result(*call("POST", f"/v1/features/{feature}/cards", {"kind": kind, "data": data}, session=True))


@_safe
def ossbot_pause(params: dict[str, Any]) -> str:
    return _result(*call("POST", "/v1/control/pause", {"reason": _text(params, "reason", 500)}))


@_safe
def ossbot_kill(params: dict[str, Any]) -> str:
    return _result(*call("POST", "/v1/control/kill", {"reason": _text(params, "reason", 500)}))


@_safe
def ossbot_feature_save(params: dict[str, Any]) -> str:
    """A record in the feature's own SQLite store (local to Hermes; no gatekeeper call)."""
    store = FeatureStore(_text(params, "feature", 40))
    data = params.get("data")
    if not isinstance(data, dict):
        raise ValueError("data must be a JSON object")
    return json.dumps({"success": True, "data": store.save(_text(params, "key", 80), data)}, ensure_ascii=False)


@_safe
def ossbot_feature_query(params: dict[str, Any]) -> str:
    store = FeatureStore(_text(params, "feature", 40))
    prefix = params.get("prefix") if isinstance(params.get("prefix"), str) else ""
    limit = params.get("limit") if isinstance(params.get("limit"), int) else 50
    return json.dumps({"success": True, "data": store.query(prefix.strip(), limit)}, ensure_ascii=False)


JOB_ID_RE = re.compile(r"^k_[0-9a-f]{16}$")
ATTACHMENT_RE = re.compile(r"^a_[0-9a-f]{16}$")
JOB_WAIT_S = 240.0
JOB_POLL_S = 2.0


@_safe
def ossbot_request_sandbox_job(params: dict[str, Any]) -> str:
    """Run a short program in a ``--network none`` sandbox and wait for its result (DESIGN §15.6)."""
    kind = params.get("kind", "code")
    if kind != "code":
        raise ValueError('kind must be "code"')
    language = params.get("language")
    if language not in ("python", "bash"):
        raise ValueError('language must be "python" or "bash"')
    code = _text(params, "code", 64 * 1024)
    files = params.get("files") or []
    if not isinstance(files, list) or len(files) > 5 or not all(isinstance(f, str) and ATTACHMENT_RE.match(f)
                                                                for f in files):  # fmt: skip
        raise ValueError("files must be at most 5 attachment ids (a_...)")
    timeout_s = params.get("timeout_s", 120)
    if not isinstance(timeout_s, int) or not 1 <= timeout_s <= 120:
        raise ValueError("timeout_s must be 1..120")
    body = {"kind": "code", "language": language, "code": code, "files": files, "timeout_s": timeout_s}
    status, data = call("POST", "/v1/chat/sandbox-jobs", body)
    if status != 202 or not isinstance(data, dict) or not JOB_ID_RE.match(str(data.get("job_id"))):
        return _result(status, data)
    job_id = str(data["job_id"])
    deadline = time.monotonic() + JOB_WAIT_S
    while time.monotonic() < deadline:
        status, data = call("GET", f"/v1/chat/sandbox-jobs/{job_id}")
        if status != 200 or not isinstance(data, dict) or data.get("status") not in ("queued", "running"):
            return _result(status, data)
        time.sleep(JOB_POLL_S)
    return json.dumps({"success": False, "error": "still running", "job_id": job_id})


def _schema(
    name: str, description: str, props: dict[str, Any] | None = None, required: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": props or {}, "required": list(required)},
    }


_STR = {"type": "string"}
TOOLS: list[tuple[dict[str, Any], Any]] = [
    (
        _schema("ossbot_status", "ossbot state: lifecycle, kill/pause, dry-run, egress mode, Hermes tier."),
        ossbot_status,
    ),
    (_schema("ossbot_queue", "Open OSS contributions and their states."), ossbot_queue),
    (_schema("ossbot_contribution", "One contribution by id (c_...).", {"cid": _STR}, ("cid",)), ossbot_contribution),
    (
        _schema(
            "ossbot_logs", "Stage runs of a contribution, optionally one stage.", {"cid": _STR, "stage": _STR}, ("cid",)
        ),
        ossbot_logs,
    ),
    (
        _schema(
            "ossbot_request_discovery",
            "Ask ossbot to look at a GitHub issue URL of an enabled target; the owner still approves any work.",
            {"url": _STR},
            ("url",),
        ),
        ossbot_request_discovery,
    ),
    (
        _schema(
            "ossbot_notify",
            "Send the owner a short note in this conversation (P2 silent by default; P1 is loud, at most 5 a day).",
            {"text": _STR, "priority": {"type": "string", "enum": ["P1", "P2"]}},
            ("text",),
        ),
        ossbot_notify,
    ),
    (
        _schema(
            "ossbot_reminder_create",
            "Remind the owner later in this conversation. when: ISO 8601 with a zone, e.g. 2026-10-21T09:00+07:00.",
            {"when": _STR, "text": _STR},
            ("when", "text"),
        ),
        ossbot_reminder_create,
    ),
    (
        _schema(
            "ossbot_feature_card",
            "Show a card of an enabled ossbot feature in this conversation (validated by ossbot).",
            {"feature": _STR, "kind": _STR, "data": {"type": "object"}},
            ("feature", "kind", "data"),
        ),
        ossbot_feature_card,
    ),
    (
        _schema("ossbot_pause", "Pause the OSS pipeline (the owner resumes it).", {"reason": _STR}, ("reason",)),
        ossbot_pause,
    ),
    (
        _schema(
            "ossbot_kill",
            "Emergency stop of ossbot (soft kill; only the owner can resume).",
            {"reason": _STR},
            ("reason",),
        ),
        ossbot_kill,
    ),
    (
        _schema(
            "ossbot_feature_save",
            "Save one record in a personal feature store. Features: "
            + "; ".join(f"{f.name}: {f.purpose}" for f in FEATURES.values())
            + " Use a sortable key such as 2026-10-20T13:05-lunch.",
            {"feature": _STR, "key": _STR, "data": {"type": "object"}},
            ("feature", "key", "data"),
        ),
        ossbot_feature_save,
    ),
    (
        _schema(
            "ossbot_feature_query",
            "Read records of a personal feature store, newest key first, optionally by key prefix (e.g. 2026-10).",
            {"feature": _STR, "prefix": _STR, "limit": {"type": "integer"}},
            ("feature",),
        ),
        ossbot_feature_query,
    ),
    (
        _schema(
            "ossbot_request_sandbox_job",
            "Run a short python or bash program in an offline sandbox (no network, 120 s, Pillow/numpy/pandas)"
            " and get its stdout, stderr and result files. Pass the owner's attachment ids (a_...) in files;"
            " each one is then at /io/input/<id>-<file name> (read-only; the note with the attachment gives the"
            " exact path, os.listdir('/io/input') shows them all). Write result files to $OSSBOT_OUTPUT_DIR"
            " (max 5, 10 MB). Call it once per program and wait for its result.",
            {
                "kind": {"type": "string", "enum": ["code"]},
                "language": {"type": "string", "enum": ["python", "bash"]},
                "code": _STR,
                "files": {"type": "array", "items": _STR},
                "timeout_s": {"type": "integer"},
            },
            ("language", "code"),
        ),
        ossbot_request_sandbox_job,
    ),
]
