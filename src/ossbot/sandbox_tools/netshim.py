#!/usr/bin/env python3
"""ossbot-netshim: proxy-unaware tools in repo-code sandboxes reach the web through squid (build-env design
§3.2, ADR-0025 D2). Stdlib only (asyncio); baked into ``sandbox-full`` at /opt/ossbot/bin/ossbot-netshim, never
into the LLM image. Runs as the repo's own uid (10001) inside fetch, validate and (networked) agent-exec, and
can do nothing the repo code can't already do by speaking to ``proxy:3128`` itself: it is untrusted, and every
floor, the metering and the names in the log stay in squid, the kernel and the internal Docker networks.

- **Stub DNS** on 127.0.0.1:53 UDP and TCP. Docker's embedded resolver (127.0.0.11, ``--dns 127.0.0.1``)
  answers container names (``proxy``) and forwards every other query here from the container's own netns.
  One question, QCLASS IN, lower-cased LDH labels, <= 253 bytes, >= 2 labels, else NXDOMAIN; ``.invalid``,
  ``.localhost`` and ``localhost`` NXDOMAIN; ``A`` answers one stable per-name address in 127.70.0.0/16
  (TTL 60); ``AAAA`` and every other type NOERROR with no answer; malformed FORMERR; more than 60 000 names
  SERVFAIL. Nothing is ever forwarded: no DNS leaves the container.
- **Listeners** on 0.0.0.0:443 and 0.0.0.0:80. A connection whose local address isn't a mapped 127.70.x.y is
  closed. 443: ``CONNECT <name>:443`` to the proxy, then both directions are spliced (TLS, gRPC, git smart
  HTTP and raw TCP ride the tunnel; the name comes from the address, not SNI). 80: the request head (<= 64 KiB,
  30 s) is rewritten to the absolute form ``http://<name><path>`` with the ``Proxy-*`` headers dropped and
  ``Connection: close``, then spliced. 512 connections, 600 s idle, 64 KiB buffers.

Usage: ``ossbot-netshim [--daemon --pidfile F] [--log F]``. With ``--daemon`` the sockets are bound before the
fork, so a bind failure (port in use) is a non-zero exit the entrypoint reports in one stderr line.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import os
import re
import socket
import struct
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

FAKE_NET = ipaddress.IPv4Network("127.70.0.0/16")
MAX_NAMES = 60_000
TTL = 60
UDP_MAX = 512
TCP_MAX = 4096
MAX_CONNS = 512
IDLE_S = 600.0
HEAD_TIMEOUT_S = 30.0
CONNECT_REPLY_MAX = 8 * 1024
HTTP_HEAD_MAX = 64 * 1024
BUF = 64 * 1024
LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
REQUEST_LINE_RE = re.compile(r"^([A-Z]{1,16}) (/\S{0,8000}) (HTTP/1\.[01])$")
STATUS_LINE_RE = re.compile(r"^HTTP/1\.[01] (\d{3})(?: .*)?$")
NOERROR, FORMERR, SERVFAIL, NXDOMAIN = 0, 1, 2, 3
TYPE_A, CLASS_IN = 1, 1
LOG: Callable[[str], None] = lambda message: None  # noqa: E731 - replaced by --log


# ----------------------------------------------------------------------------- names
class Names:
    """Stable name <-> fake address map for the process lifetime (127.70.0.1 upwards, .0/.255 hosts skipped)."""

    def __init__(self, limit: int = MAX_NAMES) -> None:
        self.limit = limit
        self.by_name: dict[str, str] = {}
        self.by_ip: dict[str, str] = {}
        self._next = int(FAKE_NET.network_address) + 1

    def address(self, name: str) -> str | None:
        if name in self.by_name:
            return self.by_name[name]
        if len(self.by_name) >= self.limit:
            return None
        while self._next & 0xFF in (0, 255):
            self._next += 1
        ip = str(ipaddress.IPv4Address(self._next))
        if ipaddress.IPv4Address(ip) not in FAKE_NET:
            return None
        self._next += 1
        self.by_name[name], self.by_ip[ip] = ip, name
        return ip

    def name(self, ip: str) -> str | None:
        return self.by_ip.get(ip)


def valid_name(name: str) -> bool:
    if not name or len(name) > 253 or name != name.lower():
        return False
    labels = name.split(".")
    if len(labels) < 2 or name in ("localhost",) or labels[-1] in ("invalid", "localhost"):
        return False
    return all(LABEL_RE.fullmatch(label) for label in labels)


# ------------------------------------------------------------------------------- DNS
class Malformed(ValueError):
    pass


def _parse_question(msg: bytes) -> tuple[int, int, str, int, int, int]:
    """(id, flags, name, qtype, qclass, end of the question) of a standard query; raises Malformed."""
    if len(msg) < 12:
        raise Malformed("short")
    qid, flags, qd, an, ns, _ar = struct.unpack("!HHHHHH", msg[:12])
    if flags & 0x8000 or (flags >> 11) & 0xF != 0 or qd != 1 or an or ns:
        raise Malformed("not one standard question")
    labels, i = [], 12
    while True:
        if i >= len(msg):
            raise Malformed("truncated name")
        n = msg[i]
        i += 1
        if n == 0:
            break
        if n & 0xC0:
            raise Malformed("compression in a question")
        if i + n > len(msg):
            raise Malformed("truncated label")
        labels.append(msg[i : i + n])
        i += n
    if i + 4 > len(msg):
        raise Malformed("truncated question")
    qtype, qclass = struct.unpack("!HH", msg[i : i + 4])
    try:
        name = b".".join(labels).decode("ascii").lower()
    except UnicodeDecodeError as exc:
        raise Malformed("non-ascii name") from exc
    return qid, flags, name, qtype, qclass, i + 4


def _header(qid: int, flags: int, rcode: int, qd: int, an: int) -> bytes:
    rd = flags & 0x0100
    return struct.pack("!HHHHHH", qid, 0x8000 | 0x0080 | rd | rcode, qd, an, 0, 0)


def answer(msg: bytes, names: Names) -> bytes | None:
    """The response to one DNS message (None: not even a header to answer)."""
    if len(msg) < 12:
        return None
    try:
        qid, flags, name, qtype, qclass, end = _parse_question(msg)
    except Malformed:
        qid, flags = struct.unpack("!HH", msg[:4])
        return _header(qid, flags, FORMERR, 0, 0)
    question = msg[12:end]
    if qclass != CLASS_IN:
        return _header(qid, flags, FORMERR, 0, 0)
    if not valid_name(name):
        return _header(qid, flags, NXDOMAIN, 1, 0) + question
    if qtype != TYPE_A:
        return _header(qid, flags, NOERROR, 1, 0) + question  # AAAA and the rest: no IPv6 path exists
    ip = names.address(name)
    if ip is None:
        return _header(qid, flags, SERVFAIL, 1, 0) + question
    rr = struct.pack("!HHHIH", 0xC00C, TYPE_A, CLASS_IN, TTL, 4) + socket.inet_aton(ip)
    return _header(qid, flags, NOERROR, 1, 1) + question + rr


class DnsUdp(asyncio.DatagramProtocol):
    def __init__(self, names: Names) -> None:
        self.names = names
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        if len(data) > UDP_MAX:
            reply = answer(data[:12], self.names) if len(data) >= 12 else None  # oversize: FORMERR
        else:
            reply = answer(data, self.names)
        if reply is not None and self.transport is not None:
            self.transport.sendto(reply, addr)


async def dns_tcp(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, names: Names) -> None:
    try:
        while True:
            head = await asyncio.wait_for(reader.readexactly(2), timeout=10)
            (n,) = struct.unpack("!H", head)
            if n > TCP_MAX:
                return
            msg = await asyncio.wait_for(reader.readexactly(n), timeout=10)
            reply = answer(msg, names)
            if reply is None:
                return
            writer.write(struct.pack("!H", len(reply)) + reply)
            await writer.drain()
    except (asyncio.IncompleteReadError, TimeoutError, OSError):
        return
    finally:
        writer.close()


# ---------------------------------------------------------------------------- tunnels
def proxy_address(env: dict[str, str] | None = None) -> tuple[str, int]:
    e = os.environ if env is None else env
    url = e.get("HTTPS_PROXY") or e.get("HTTP_PROXY") or "http://proxy:3128"
    parts = urlsplit(url)
    return parts.hostname or "proxy", parts.port or 3128


async def _read_head(reader: asyncio.StreamReader, limit: int) -> tuple[bytes, bytes]:
    """(head including the blank line, any bytes after it); raises ValueError over ``limit``."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = await reader.read(BUF)
        if not chunk:
            raise ValueError("eof before the end of the head")
        buf += chunk
        if len(buf) > limit and b"\r\n\r\n" not in buf[:limit]:
            raise ValueError("head too large")
    end = buf.index(b"\r\n\r\n") + 4
    if end > limit:
        raise ValueError("head too large")
    return buf[:end], buf[end:]


def rewrite_http_head(head: bytes, name: str) -> bytes | None:
    """The absolute-form request for the proxy, or None for anything but an origin-form HTTP/1.x request."""
    try:
        text = head.decode("latin-1")
    except UnicodeDecodeError:  # pragma: no cover - latin-1 decodes every byte
        return None
    lines = text.split("\r\n")
    m = REQUEST_LINE_RE.fullmatch(lines[0])
    if m is None:
        return None
    method, path, version = m.groups()
    kept = [ln for ln in lines[1:] if ln and not ln.lower().startswith(("proxy-", "connection:"))]
    out = [f"{method} http://{name}{path} {version}", *kept, "Connection: close", "", ""]
    return "\r\n".join(out).encode("latin-1")


async def splice(a_r: asyncio.StreamReader, a_w: asyncio.StreamWriter, b_r: asyncio.StreamReader,
                 b_w: asyncio.StreamWriter, idle_s: float = IDLE_S) -> None:  # fmt: skip
    last = [time.monotonic()]

    async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(src.read(BUF), timeout=idle_s)
                except TimeoutError:
                    if time.monotonic() - last[0] >= idle_s:
                        return  # the pair was idle in both directions
                    continue
                if not chunk:
                    with contextlib.suppress(OSError, RuntimeError):
                        if dst.can_write_eof():
                            dst.write_eof()
                    return
                last[0] = time.monotonic()
                dst.write(chunk)
                await dst.drain()
        except (OSError, RuntimeError):
            return

    tasks = [asyncio.ensure_future(pump(a_r, b_w)), asyncio.ensure_future(pump(b_r, a_w))]
    try:
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if pending:  # one side ended: give the other the idle time to finish its direction
            await asyncio.wait(pending, timeout=idle_s)
    finally:
        for t in tasks:
            t.cancel()
        for w in (a_w, b_w):
            w.close()


class Shim:
    def __init__(self, names: Names, proxy: tuple[str, int], max_conns: int = MAX_CONNS) -> None:
        self.names = names
        self.proxy = proxy
        self.max_conns = max_conns
        self.active = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, port: int) -> None:
        local = writer.get_extra_info("sockname")
        name = self.names.name(str(local[0])) if local else None
        if name is None or self.active >= self.max_conns:  # not a mapped address (an IP literal, 127.0.0.1)
            writer.close()
            return
        self.active += 1
        try:
            await self._tunnel(reader, writer, name, port)
        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError) as exc:
            LOG(f"{port} {name}: {type(exc).__name__}")
            writer.close()
        finally:
            self.active -= 1

    async def _tunnel(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, name: str,
                      port: int) -> None:  # fmt: skip
        if port == 80:
            head, rest = await asyncio.wait_for(_read_head(reader, HTTP_HEAD_MAX), timeout=HEAD_TIMEOUT_S)
            request = rewrite_http_head(head, name)
            if request is None:
                LOG(f"80 {name}: not an HTTP/1.x origin-form request")
                writer.close()
                return
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(*self.proxy), timeout=HEAD_TIMEOUT_S)
            up_w.write(request + rest)
            await up_w.drain()
            await splice(reader, writer, up_r, up_w)
            return
        up_r, up_w = await asyncio.wait_for(asyncio.open_connection(*self.proxy), timeout=HEAD_TIMEOUT_S)
        up_w.write(f"CONNECT {name}:443 HTTP/1.1\r\nHost: {name}:443\r\n\r\n".encode("ascii"))
        await up_w.drain()
        reply, rest = await asyncio.wait_for(_read_head(up_r, CONNECT_REPLY_MAX), timeout=HEAD_TIMEOUT_S)
        status = STATUS_LINE_RE.fullmatch(reply.decode("latin-1").split("\r\n", 1)[0])
        if status is None or status.group(1) != "200":
            LOG(f"443 {name}: proxy answered {status.group(1) if status else 'garbage'}")
            up_w.close()
            writer.close()
            return
        if rest:
            writer.write(rest)
        await splice(reader, writer, up_r, up_w)


# ------------------------------------------------------------------------------- main
def bind_sockets(host: str = "0.0.0.0", dns_host: str = "127.0.0.1", tcp_ports: tuple[int, int] = (443, 80),  # noqa: S104
                 dns_port: int = 53) -> dict[str, socket.socket]:  # fmt: skip
    """Bind every socket first (a failure is reported before any fork)."""
    socks: dict[str, socket.socket] = {}
    try:
        for port in tcp_ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            s.listen(128)
            s.setblocking(False)
            socks[f"tcp{port}"] = s
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.bind((dns_host, dns_port))
        u.setblocking(False)
        socks["dns_udp"] = u
        t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        t.bind((dns_host, dns_port))
        t.listen(64)
        t.setblocking(False)
        socks["dns_tcp"] = t
    except OSError:
        for s in socks.values():
            s.close()
        raise
    return socks


async def serve(socks: dict[str, socket.socket], names: Names, proxy: tuple[str, int]) -> None:
    loop = asyncio.get_running_loop()
    shim = Shim(names, proxy)
    servers = []

    def on_port(port: int) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await shim.handle(r, w, port)

        return handler

    async def on_dns(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await dns_tcp(r, w, names)

    for key, sock in socks.items():
        if key.startswith("tcp"):
            servers.append(await asyncio.start_server(on_port(int(key[3:])), sock=sock))
    await loop.create_datagram_endpoint(lambda: DnsUdp(names), sock=socks["dns_udp"])
    servers.append(await asyncio.start_server(on_dns, sock=socks["dns_tcp"]))
    await asyncio.gather(*(s.serve_forever() for s in servers))


def main(argv: list[str] | None = None) -> int:
    global LOG  # noqa: PLW0603 - the one process-wide log sink
    parser = argparse.ArgumentParser(prog="ossbot-netshim")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--pidfile", default=None)
    parser.add_argument("--log", default=None)
    args = parser.parse_args(argv)
    try:
        socks = bind_sockets()
    except OSError as exc:
        print(f"ossbot-netshim: cannot bind ({exc.strerror}); proxy-unaware tools get no route", file=sys.stderr)
        return 1
    if args.daemon:
        pid = os.fork()
        if pid:
            if args.pidfile:
                with open(args.pidfile, "w", encoding="ascii") as fh:
                    fh.write(f"{pid}\n")
            return 0
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
    if args.log:
        log_fd = os.open(args.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)

        def _log(message: str) -> None:
            with contextlib.suppress(OSError):
                os.write(log_fd, f"{time.strftime('%H:%M:%S')} {message}\n".encode("utf-8", "replace"))

        LOG = _log
    asyncio.run(serve(socks, Names(), proxy_address()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
