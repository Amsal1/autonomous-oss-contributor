"""M9 surface gate (DESIGN §15.2, §22.3): a session that is not ``ossbot-chat`` or advertises a tool
outside {web_search, web_fetch} is never prompted, the turn fails and a soft kill is requested."""

from __future__ import annotations

import json
import os
import socket
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.acp.conftest import FakeRun, ask, client, v3, v3_core


@pytest.fixture
def gatekeeper_uds() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """A fake gatekeeper on a Unix socket that records ``POST /v1/control/kill`` bodies."""
    import tempfile

    tmp = tempfile.mkdtemp(prefix="gk")
    path = os.path.join(tmp, "gk.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(4)
    seen: list[dict[str, Any]] = []
    stop = threading.Event()

    def serve() -> None:
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    data += conn.recv(4096)
                head, _, body = data.partition(b"\r\n\r\n")
                length = int(
                    next(h for h in head.split(b"\r\n") if h.lower().startswith(b"content-length")).split(b":")[1]
                )
                while len(body) < length:
                    body += conn.recv(4096)
                seen.append({"line": head.split(b"\r\n")[0].decode(), "body": json.loads(body)})
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{}")

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    yield path, seen
    stop.set()
    t.join(timeout=2)
    srv.close()


def _uds_client(run: FakeRun, tmp_path: Path, path: str) -> v3.KiroV3Client:
    s = v3.V3Settings(models_file=str(tmp_path / "none.json"), violation_uds=path)
    from tests.acp.conftest import Bridge

    return v3.KiroV3Client(settings=s, spawn=run.spawn, bridge=Bridge())


@pytest.mark.parametrize(
    "session",
    [{"mode": "vibe"}, {"no_mode": True}, {"tools": ["web_search", "read"]}, {"tools": [{"name": "fs_write"}]}],
)
def test_wrong_surface_is_never_prompted_and_kills(
    fake: Callable[..., FakeRun],
    tmp_path: Path,
    gatekeeper_uds: tuple[str, list[dict[str, Any]]],
    session: dict[str, Any],
) -> None:
    path, seen = gatekeeper_uds
    run = fake(session=session)
    c = _uds_client(run, tmp_path, path)
    with pytest.raises(v3_core.SurfaceViolation) as err:
        ask(c)
    assert "chat disabled: Kiro session surface check failed" in str(err.value)
    assert "session/prompt" not in run.methods()
    assert seen == [{"line": "POST /v1/control/kill HTTP/1.1", "body": {"reason": "hermes_kiro_surface"}}]
    c.close()


def test_load_with_another_mode_is_a_violation(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    hits: list[str] = []
    run = fake(prompts=[[{"drop": True}]], load={"mode": "vibe"})
    c = client(run, tmp_path, hook=lambda exc: hits.append(exc.reason))
    with pytest.raises(v3_core.SurfaceViolation):
        ask(c)
    assert hits == ["mode"]
    assert run.methods().count("session/prompt") == 1
    c.close()


def test_mode_change_during_a_turn_fails_it(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    hits: list[str] = []
    run = fake(prompts=[[{"mode_update": "vibe"}, {"text": "secret"}]])
    c = client(run, tmp_path, hook=lambda exc: hits.append(exc.reason))
    with pytest.raises(v3_core.SurfaceViolation):
        ask(c)
    assert hits == ["mode"]
    c.close()


def test_tools_changed_outside_the_allowlist_fails_the_turn(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    hits: list[str] = []
    run = fake(prompts=[[{"tools_changed": ["web_fetch", "shell"]}, {"text": "x"}]])
    c = client(run, tmp_path, hook=lambda exc: hits.append(exc.reason))
    with pytest.raises(v3_core.SurfaceViolation):
        ask(c)
    assert hits == ["tools"]
    c.close()


def test_ossbot_chat_with_web_tools_is_served(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    hits: list[str] = []
    run = fake(
        session={"tools": ["web_search", "web_fetch"]}, prompts=[[{"tools_changed": ["web_search"]}, {"text": "fine"}]]
    )
    c = client(run, tmp_path, hook=lambda exc: hits.append(exc.reason))
    assert ask(c).choices[0].message.content == "fine"
    assert hits == []
    c.close()


def test_assert_surface_unit() -> None:
    ok = {"modes": {"currentModeId": "ossbot-chat"}}
    v3_core.assert_surface(ok)
    v3_core.assert_surface({**ok, "tools": ["web_fetch"]})
    v3_core.assert_surface({"configOptions": [{"id": "mode", "currentValue": "ossbot-chat"}]})
    for bad in ({"modes": {"currentModeId": "vibe"}}, {}, {**ok, "tools": ["web_search", "execute_bash"]}):
        with pytest.raises(v3_core.SurfaceViolation):
            v3_core.assert_surface(bad)
