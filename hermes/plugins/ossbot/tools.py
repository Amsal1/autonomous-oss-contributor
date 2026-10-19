"""ossbot tools for Hermes (DESIGN §15.1): read status and file requests over the gatekeeper UDS.

The gatekeeper derives the caller's role from the socket peer uid (``hermes`` = requester), so these
tools can read status, file a discovery request for an issue URL, send the owner a note, pause or
kill; they cannot approve, submit, resume or change settings. Stdlib only; never imports ossbot.
Every result is a JSON string; gatekeeper errors are returned as ``{"success": false, ...}``.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import urllib.parse
from typing import Any

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


def call(method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float = 20.0) -> tuple[int, Any]:
    conn = _UDS(SOCKET, timeout)
    try:
        payload = None if body is None else json.dumps(body)
        headers = {"Content-Type": "application/json"} if payload is not None else {}
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
    return _result(*call("POST", "/v1/notify", {"text": _text(params, "text", 4000)}))


@_safe
def ossbot_pause(params: dict[str, Any]) -> str:
    return _result(*call("POST", "/v1/control/pause", {"reason": _text(params, "reason", 500)}))


@_safe
def ossbot_kill(params: dict[str, Any]) -> str:
    return _result(*call("POST", "/v1/control/kill", {"reason": _text(params, "reason", 500)}))


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
        _schema("ossbot_notify", "Send the owner a short note in the ossbot chat.", {"text": _STR}, ("text",)),
        ossbot_notify,
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
]
