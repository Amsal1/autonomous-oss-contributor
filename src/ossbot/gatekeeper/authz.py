"""Caller roles from ``SO_PEERCRED`` (DESIGN §12.2). The role comes from the peer uid, never a header.

uvicorn builds the ASGI app per connection (``self.app`` on the protocol), so
``PeerCredH11Protocol`` reads the peer credentials once at ``connection_made`` and wraps the
connection's app to put them into every request scope as ``scope["ossbot.peer"]``.
"""

from __future__ import annotations

import asyncio
import logging
import pwd
import socket
import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from fastapi import HTTPException, Request
from uvicorn.protocols.http.h11_impl import H11Protocol

log = logging.getLogger(__name__)
PEER_SCOPE_KEY = "ossbot.peer"


class Role(StrEnum):
    OPERATOR = "operator"
    WORKER = "worker"
    SANDBOX = "sandbox"
    REQUESTER = "requester"


ALL_ROLES: frozenset[Role] = frozenset(Role)
ROLE_USERS: Mapping[str, Role] = {
    "root": Role.OPERATOR,
    "ossadmin": Role.OPERATOR,
    "ossrunner": Role.WORKER,
    "ossbox": Role.SANDBOX,
    "hermes": Role.REQUESTER,
}


@dataclass(frozen=True)
class Peer:
    pid: int
    uid: int
    gid: int


@dataclass(frozen=True)
class Caller:
    peer: Peer
    role: Role

    @property
    def actor(self) -> str:
        return f"uid:{self.peer.uid}/{self.role.value}"


def resolve_role_map(users: Mapping[str, Role] = ROLE_USERS) -> dict[int, Role]:
    """Map uids of the known accounts; accounts that do not exist are simply absent (-> 403)."""
    out: dict[int, Role] = {}
    for name, role in users.items():
        try:
            out[pwd.getpwnam(name).pw_uid] = role
        except KeyError:
            log.warning("role account %s does not exist", name)
    return out


def peercred(sock: socket.socket) -> Peer:
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    return Peer(pid=pid, uid=uid, gid=gid)


ASGIApp = Callable[[dict[str, Any], Callable[..., Any], Callable[..., Any]], Any]


class _PeerApp:
    def __init__(self, app: ASGIApp, peer: Peer | None) -> None:
        self.app = app
        self.peer = peer

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope.get("type") in {"http", "websocket"}:
            scope = dict(scope)
            scope[PEER_SCOPE_KEY] = self.peer
        await self.app(scope, receive, send)


class PeerCredH11Protocol(H11Protocol):
    def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        sock = transport.get_extra_info("socket")
        peer: Peer | None = None
        if sock is not None and sock.family == socket.AF_UNIX:
            try:
                peer = peercred(sock)
            except OSError:
                peer = None
        self.app = _PeerApp(self.app, peer)


def caller_from_scope(scope: Mapping[str, Any], roles: Mapping[int, Role]) -> Caller | None:
    peer = scope.get(PEER_SCOPE_KEY)
    if not isinstance(peer, Peer):
        return None
    role = roles.get(peer.uid)
    return Caller(peer=peer, role=role) if role is not None else None


class Forbidden(HTTPException):
    def __init__(self, detail: str, uid: int | None) -> None:
        super().__init__(status_code=403, detail=detail)
        self.uid = uid


def require(*allowed: Role) -> Callable[[Request], Caller]:
    allowed_set = frozenset(allowed) if allowed else ALL_ROLES

    def dependency(request: Request) -> Caller:
        roles: Mapping[int, Role] = request.app.state.ctx.roles
        caller = caller_from_scope(request.scope, roles)
        peer = request.scope.get(PEER_SCOPE_KEY)
        uid = peer.uid if isinstance(peer, Peer) else None
        if caller is None:
            raise Forbidden("unknown caller", uid)
        if caller.role not in allowed_set:
            raise Forbidden(f"role {caller.role.value} may not call this endpoint", uid)
        return caller

    dependency.allowed_roles = allowed_set  # type: ignore[attr-defined]
    return dependency


def interactive_ancestry(pid: int, proc: str = "/proc", max_depth: int = 64) -> bool:
    """Best effort: does the caller's process tree have an ancestor with a controlling TTY?"""
    current = pid
    for _ in range(max_depth):
        if current <= 1:
            return False
        try:
            with open(f"{proc}/{current}/stat", encoding="utf-8") as fh:
                stat = fh.read()
        except OSError:
            return False
        fields = stat.rsplit(")", 1)[-1].split()
        try:
            ppid, tty_nr = int(fields[1]), int(fields[4])
        except (IndexError, ValueError):
            return False
        if tty_nr != 0:
            return True
        current = ppid
    return False
