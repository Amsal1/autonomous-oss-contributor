"""The real stack: uvicorn on a temp Unix socket, roles from the kernel's SO_PEERCRED (§12.2)."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from ossbot.common.uds import uds_client
from ossbot.gatekeeper.app import create_app
from ossbot.gatekeeper.authz import PeerCredH11Protocol, Role
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.main import bind_socket


@pytest.fixture
def served(ctx: AppContext, tmp_path: Path) -> Iterator[Path]:
    sock_path = Path(f"/tmp/ossbot-test-{os.getpid()}-{time.monotonic_ns()}.sock")  # AF_UNIX path length limit
    sock = bind_socket(sock_path, group=None)
    config = uvicorn.Config(create_app(ctx, housekeeping=False), http=PeerCredH11Protocol, lifespan="off",
                            log_config=None, access_log=False)  # fmt: skip
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    try:
        yield sock_path
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
        sock_path.unlink(missing_ok=True)


def test_socket_mode_is_0660(served: Path) -> None:
    assert served.stat().st_mode & 0o777 == 0o660


def test_role_comes_from_peer_uid(ctx: AppContext, served: Path) -> None:
    ctx.roles = {os.getuid(): Role.OPERATOR}  # type: ignore[misc]
    with uds_client(served) as c:
        assert c.get("/v1/health").json() == {"ok": True}
        assert c.get("/v1/control").json()["dry_run"] is True
        # a header cannot change the role
        assert c.post("/v1/contributions", json={}, headers={"X-Role": "worker"}).status_code == 403


def test_unknown_uid_is_forbidden(ctx: AppContext, served: Path) -> None:
    ctx.roles = {}  # type: ignore[misc]
    with uds_client(served) as c:
        assert c.get("/v1/health").status_code == 403


def test_worker_uid_over_uds(ctx: AppContext, served: Path) -> None:
    ctx.roles = {os.getuid(): Role.WORKER}  # type: ignore[misc]
    with uds_client(served) as c:
        assert c.post("/v1/control/resume").status_code == 403
        assert c.post("/v1/control/pause", json={"reason": "anomaly"}).json()["state"] == "paused"
