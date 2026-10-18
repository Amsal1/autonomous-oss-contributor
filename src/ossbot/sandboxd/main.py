"""``ossbot-sandboxd`` entrypoint (runs as ``ossbox``; systemd unit in FEAT-004).

Start: open ``sandboxd.db`` -> bind ``/run/ossbot/sandboxd.sock`` (0660, group osssbx) -> kill
watch -> serve. Docker access is only through the root filter socket. ``KIRO_API_KEY`` (the
sandbox key, from ``/etc/ossbot/secrets/sandboxd.env``) is never logged; without it Kiro and
catalog jobs fail inside the container and the key scanner has nothing to look for.
"""

from __future__ import annotations

import grp
import logging
import os
import pwd
import socket
import stat
import sys
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from ossbot.common.clock import SystemClock
from ossbot.common.config import ConfigError, parse_bundle
from ossbot.common.control_flags import DEFAULT_DRAIN_TIMEOUT_S
from ossbot.common.images import DEFAULT_LOCK_COPY, load_image_pins
from ossbot.common.logging import setup_logging
from ossbot.common.paths import Paths
from ossbot.common.redact import Redactor
from ossbot.common.snapshot import read_current
from ossbot.common.uds import uds_client
from ossbot.sandboxd.app import PeerCredProtocol, create_app
from ossbot.sandboxd.db import DEFAULT_DB, Store
from ossbot.sandboxd.jobs import JobService, ProbeRunner, Settings
from ossbot.sandboxd.keyscan import KeyScanner
from ossbot.sandboxd.killwatch import KillWatch
from ossbot.sandboxd.models import CatalogCache
from ossbot.sandboxd.networks import Networks
from ossbot.sandboxd.probes import TrustMode
from ossbot.sandboxd.runtime.docker_cli import DockerCliRuntime
from ossbot.sandboxd.subnets import SubnetAllocator

log = logging.getLogger("ossbot.sandboxd")
SOCKET = Path("/run/ossbot/sandboxd.sock")
SOCKET_GROUP = "osssbx"
PROXY_ACCESS_LOG = Path("/srv/ossbot/logs/proxy/access.log")


class GatekeeperClient:
    def __init__(self, sock: Path) -> None:
        self.sock = sock

    def _client(self) -> httpx.Client:
        return uds_client(self.sock)

    def catalog(self) -> list[dict[str, Any]]:
        with self._client() as c:
            resp = c.get("/v1/models/catalog")
            resp.raise_for_status()
            data = resp.json()
        return data if isinstance(data, list) else []

    def pause(self, reason: str) -> None:
        with self._client() as c:
            c.post("/v1/control/pause", json={"reason": reason[:500]}).raise_for_status()

    def contribution_state(self, cid: str) -> str | None:
        with self._client() as c:
            resp = c.get(f"/v1/contributions/{cid}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            state = resp.json().get("state")
        return state if isinstance(state, str) else None

    def probe_result(self, probe_key: str, probe: str, passed: bool, evidence_sha256: str, *,
                     verdict: str | None = None, attempts: int = 1) -> None:  # fmt: skip
        body = {"probe_key": probe_key, "probe": probe, "passed": passed, "evidence_sha256": evidence_sha256,
                "verdict": verdict, "attempts": attempts}  # fmt: skip
        with self._client() as c:
            c.post("/v1/sandbox/probe-result", json=body).raise_for_status()


def trust_mode_from_snapshot(root: Path) -> TrustMode:
    """``policy.yaml sandbox.kiro_trust_mode`` from the gatekeeper snapshot; unreadable -> ids."""
    try:
        _, files = read_current(root)
        return parse_bundle(files).policy.sandbox.kiro_trust_mode
    except (OSError, ValueError, ConfigError):
        log.warning("config snapshot unreadable; using kiro_trust_mode=ids")
        return "ids"


def bind_socket(path: Path, group: str) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if stat.S_ISSOCK(os.lstat(path).st_mode):
            path.unlink()
        else:
            raise RuntimeError(f"{path} exists and is not a socket")
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o117)
    try:
        sock.bind(str(path))
    finally:
        os.umask(old)
    try:
        os.chown(path, -1, grp.getgrnam(group).gr_gid)
    except (KeyError, PermissionError):
        log.warning("could not set group %s on %s", group, path)
    os.chmod(path, 0o660)
    sock.listen(64)
    return sock


def _allowed_uids() -> frozenset[int]:
    uids = {0}
    try:
        uids.add(pwd.getpwnam("ossrunner").pw_uid)
    except KeyError:
        log.warning("account ossrunner does not exist")
    return frozenset(uids)


def _proxy_log() -> str:
    try:
        return PROXY_ACCESS_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unreadable proxy log: canary.invalid cannot be ruled out"  # fail closed for 'workspace'


def main() -> int:
    key = os.environ.get("KIRO_API_KEY") or ""
    redactor = Redactor()
    if key:
        redactor.add_secret(key)
    setup_logging("sandboxd", redactor)
    if not key:
        log.warning("no KIRO_API_KEY: Kiro and catalog jobs will fail (awaiting secret)")
    paths = Paths.default()
    clock = SystemClock()
    store = Store(DEFAULT_DB)
    runtime = DockerCliRuntime()
    gatekeeper = GatekeeperClient(paths.gatekeeper_sock)
    jobs = JobService(
        store=store, runtime=runtime, pins=lambda: load_image_pins(DEFAULT_LOCK_COPY),
        catalog=CatalogCache(gatekeeper.catalog, clock), gatekeeper=gatekeeper,
        scanner=KeyScanner([key] if key else []), clock=clock,
        settings=Settings(
            artifacts_dir=paths.artifacts_dir, logs_dir=paths.logs_dir, staging_dir=paths.srv / "sandboxd" / "staging",
            control_dir=paths.control_dir, input_roots=(paths.srv / "runner", paths.artifacts_dir),
        ),
        trust_mode=lambda: trust_mode_from_snapshot(paths.snapshot_root),
        networks=Networks(runtime, SubnetAllocator(store, clock)),
    )  # fmt: skip
    jobs.recover()  # CR-001 crash safety: whatever a killed/crashed predecessor left behind
    watch = KillWatch(jobs, paths.control_dir)
    watch.start()
    sock = bind_socket(SOCKET, SOCKET_GROUP)
    app = create_app(jobs, ProbeRunner(jobs, _proxy_log), _allowed_uids())
    config = uvicorn.Config(
        app, http=PeerCredProtocol, lifespan="off", log_config=None, access_log=False, server_header=False,
        timeout_graceful_shutdown=5,
    )  # fmt: skip
    uvicorn.Server(config).run(sockets=[sock])
    # SIGTERM (systemctl stop): drain like `ossctl down` would (skipped while killed: the kill watch
    # already killed the containers). TimeoutStopSec in the unit is >= this + 30 s.
    result = jobs.drain(drain_timeout())
    log.info("stopped: %d finished, %d interrupted", result["finished"], result["interrupted"])
    watch.stop()
    return 0


def drain_timeout() -> int:
    raw = os.environ.get("OSSBOT_DRAIN_TIMEOUT_S", str(DEFAULT_DRAIN_TIMEOUT_S))
    try:
        return max(0, min(int(raw), 3600))
    except ValueError:
        return DEFAULT_DRAIN_TIMEOUT_S


if __name__ == "__main__":
    sys.exit(main())
