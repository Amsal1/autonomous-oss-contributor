"""``ossbot-dockerfilter``: root Docker API filter on a Unix socket (DESIGN §10.8).

Listens on ``/run/ossbot/docker-filter.sock`` (root:ossdock 0660), parses every HTTP request
with h11, and forwards it to ``/var/run/docker.sock`` only if ``rules.decide`` allows it;
otherwise it answers 403 with a Docker-style ``{"message": ...}`` body and logs WARNING with the
method, path and reason. Ids in requests are resolved by inspects the filter performs itself on
the real socket. One thread per client connection; responses (including streaming ``wait`` and
``logs``) are relayed event by event.
"""

from __future__ import annotations

import argparse
import grp
import http.client
import json
import logging
import os
import socket
import stat
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import h11

from ossbot.common.logging import setup_logging
from ossbot.dockerfilter import rules

log = logging.getLogger("ossbot.dockerfilter")

LISTEN = Path("/run/ossbot/docker-filter.sock")
DOCKER = Path("/var/run/docker.sock")
CONTROL_DIR = Path("/var/lib/ossbot/control")
# Root-owned copy of config/versions.lock.yaml written by the provision 'images' step (FEAT-004).
IMAGES_LOCK = Path("/etc/ossbot/versions.lock.yaml")
SOCKET_GROUP = "ossdock"
MAX_BODY = 128 * 1024 * 1024
MAX_CONNECTIONS = 64
HOP_BY_HOP = {b"connection", b"keep-alive", b"proxy-connection", b"transfer-encoding", b"upgrade", b"te", b"trailer"}
READ_CHUNK = 65536


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float = 10.0) -> None:
        super().__init__("docker", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(str(self._path))
        self.sock = sock


class DockerLookup:
    """Inspects on the real socket (the filter is root); never trusts what the client claims."""

    def __init__(self, docker: Path) -> None:
        self.docker = docker

    def _get(self, path: str) -> dict[str, Any] | None:
        conn = _UnixHTTPConnection(self.docker)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            data = resp.read(4 * 1024 * 1024)
            if resp.status != 200:
                return None
            doc = json.loads(data)
            return doc if isinstance(doc, dict) else None
        except (OSError, ValueError, http.client.HTTPException):
            return None
        finally:
            conn.close()

    def container(self, ref: str) -> rules.ContainerFacts | None:
        doc = self._get(f"/containers/{ref}/json")
        if doc is None:
            return None
        config = doc.get("Config") or {}
        labels = config.get("Labels") or {}
        state = doc.get("State") or {}
        if not isinstance(labels, dict) or not isinstance(state, dict):
            return None
        return rules.ContainerFacts(
            id=str(doc.get("Id", "")),
            name=str(doc.get("Name", "")).lstrip("/"),
            labels={str(k): str(v) for k, v in labels.items()},
            status=str(state.get("Status", "")),
        )

    def network(self, ref: str) -> rules.NetworkFacts | None:
        doc = self._get(f"/networks/{ref}")
        if doc is None:
            return None
        return rules.NetworkFacts(id=str(doc.get("Id", "")), name=str(doc.get("Name", "")))


PolicyProvider = Callable[[], rules.FilterPolicy]


def _send(conn: h11.Connection, sock: socket.socket, event: h11.Event) -> None:
    data = conn.send(event)
    if data:
        sock.sendall(data)


def _next_event(conn: h11.Connection, sock: socket.socket) -> h11.Event | type[h11.PAUSED] | type[h11.NEED_DATA]:
    while True:
        event = conn.next_event()
        if event is not h11.NEED_DATA:
            return event
        # After EOF (b"") h11 reports ConnectionClosed or raises RemoteProtocolError next time.
        conn.receive_data(sock.recv(READ_CHUNK))


def _read_request(conn: h11.Connection, sock: socket.socket) -> tuple[h11.Request, bytes] | None:
    request: h11.Request | None = None
    body = bytearray()
    while True:
        event = _next_event(conn, sock)
        if isinstance(event, h11.Request):
            request = event
        elif isinstance(event, h11.Data):
            body.extend(event.data)
            if len(body) > MAX_BODY:
                raise rules.Denied("body_too_large")
        elif isinstance(event, h11.EndOfMessage):
            return (request, bytes(body)) if request is not None else None
        elif isinstance(event, h11.ConnectionClosed) or event is h11.PAUSED:
            return None


def _deny(conn: h11.Connection, sock: socket.socket, reason: str) -> None:
    payload = json.dumps({"message": f"ossbot-dockerfilter: denied ({reason})"}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    _send(conn, sock, h11.Response(status_code=403, headers=headers))
    _send(conn, sock, h11.Data(data=payload))
    _send(conn, sock, h11.EndOfMessage())


def _forward(
    client: h11.Connection, client_sock: socket.socket, request: h11.Request, body: bytes, docker: Path
) -> None:
    headers = [(k, v) for k, v in request.headers if k.lower() not in HOP_BY_HOP and k.lower() != b"content-length"]
    headers = [(k, v) for k, v in headers if k.lower() != b"host"]
    headers += [(b"host", b"docker"), (b"content-length", str(len(body)).encode())]
    upstream = h11.Connection(h11.CLIENT)
    usock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        usock.connect(str(docker))
    except OSError:
        payload = b'{"message":"ossbot-dockerfilter: docker unavailable"}'
        _send(client, client_sock, h11.Response(status_code=502, headers=[(b"content-length", b"%d" % len(payload))]))
        _send(client, client_sock, h11.Data(data=payload))
        _send(client, client_sock, h11.EndOfMessage())
        return
    with usock:
        _send(upstream, usock, h11.Request(method=request.method, target=request.target, headers=headers))
        if body:
            _send(upstream, usock, h11.Data(data=body))
        _send(upstream, usock, h11.EndOfMessage())
        while True:  # h11 knows a HEAD response has no body
            event = _next_event(upstream, usock)
            if isinstance(event, h11.Response):
                resp_headers = [(k, v) for k, v in event.headers if k.lower() not in HOP_BY_HOP]
                _send(client, client_sock, h11.Response(status_code=event.status_code, headers=resp_headers))
            elif isinstance(event, h11.InformationalResponse):
                continue
            elif isinstance(event, h11.Data):
                _send(client, client_sock, h11.Data(data=event.data))
            elif isinstance(event, h11.EndOfMessage):
                _send(client, client_sock, h11.EndOfMessage())
                return
            else:
                raise ConnectionError("docker closed the connection mid-response")


def handle_connection(sock: socket.socket, docker: Path, lookup: rules.Lookup, policy: PolicyProvider) -> None:
    conn = h11.Connection(h11.SERVER, max_incomplete_event_size=64 * 1024)
    with sock:
        while True:
            try:
                got = _read_request(conn, sock)
            except rules.Denied as exc:
                log.warning("denied: %s", exc.reason, extra={"event": "docker_filter_denied"})
                return
            except (h11.RemoteProtocolError, OSError):
                return
            if got is None:
                return
            request, body = got
            target = request.target.decode("latin-1")
            path, _, raw_query = target.partition("?")
            method = request.method.decode("ascii", "replace")
            upgrade = any(k.lower() == b"upgrade" for k, _ in request.headers)
            try:
                if "%" in path or upgrade:
                    raise rules.Denied("path_encoding" if "%" in path else "upgrade")
                req = rules.Request(method=method, path=path, query=rules.parse_query(raw_query), body=body)
                decision = rules.decide(req, lookup, policy())
            except rules.Denied as exc:
                decision = rules.Decision(False, exc.reason, f"{method} {path}")
            try:
                if decision.allow:
                    log.info("%s %s", method, path, extra={"event": "docker_filter_allowed", "op": decision.operation})
                    _forward(conn, sock, request, body, docker)
                else:
                    log.warning(
                        "denied %s %s: %s", method, path, decision.reason,
                        extra={"event": "docker_filter_denied", "op": decision.operation},
                    )  # fmt: skip
                    _deny(conn, sock, decision.reason)
            except (h11.LocalProtocolError, h11.RemoteProtocolError, OSError, ConnectionError):
                return
            if conn.our_state is h11.MUST_CLOSE or conn.their_state is h11.MUST_CLOSE:
                return
            try:
                conn.start_next_cycle()
            except h11.LocalProtocolError:
                return


def bind_socket(path: Path, group: str | None) -> socket.socket:
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
    if group is not None:
        try:
            os.chown(path, 0, grp.getgrnam(group).gr_gid)
        except KeyError:
            log.warning("group %s does not exist; socket stays root-only", group)
            os.chmod(path, 0o600)
            sock.listen(64)
            return sock
    os.chmod(path, 0o660)
    sock.listen(64)
    return sock


def serve(listen: Path, docker: Path, policy: PolicyProvider, group: str | None = SOCKET_GROUP) -> None:
    server = bind_socket(listen, group)
    lookup = DockerLookup(docker)
    slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
    log.info("listening on %s -> %s", listen, docker, extra={"event": "started"})
    while True:
        client, _ = server.accept()
        if not slots.acquire(blocking=False):
            client.close()
            continue

        def run(sock: socket.socket = client) -> None:
            try:
                handle_connection(sock, docker, lookup, policy)
            finally:
                slots.release()

        threading.Thread(target=run, daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ossbot-dockerfilter")
    parser.add_argument("--listen", type=Path, default=LISTEN)
    parser.add_argument("--docker", type=Path, default=DOCKER)
    parser.add_argument("--control-dir", type=Path, default=CONTROL_DIR)
    parser.add_argument("--images-lock", type=Path, default=IMAGES_LOCK)
    args = parser.parse_args(argv)
    setup_logging("dockerfilter")
    if os.geteuid() != 0:
        log.error("must run as root")
        return 2

    def policy() -> rules.FilterPolicy:
        # Re-read per request: the 'images' step may rebuild; a missing file allows no image.
        return rules.FilterPolicy(rules.load_image_allowlist(args.images_lock), args.control_dir)

    serve(args.listen, args.docker, policy)
    return 0


if __name__ == "__main__":
    sys.exit(main())
