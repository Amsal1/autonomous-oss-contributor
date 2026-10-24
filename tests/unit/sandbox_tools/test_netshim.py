"""ossbot-netshim (build-env design §3.2): the stub DNS (rules, limits, never forwards), stable fake addresses,
the 443 CONNECT and 80 absolute-form translation against an asyncio fake proxy, refusal of unmapped local
addresses, the connection cap."""

from __future__ import annotations

import asyncio
import socket
import struct
from typing import Any

import pytest

from ossbot.sandbox_tools import netshim

pytestmark = pytest.mark.unit


def _query(name: str, qtype: int = 1, qclass: int = 1, qid: int = 0x1234, flags: int = 0x0100) -> bytes:
    labels = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".") if p) + b"\x00"
    return struct.pack("!HHHHHH", qid, flags, 1, 0, 0, 0) + labels + struct.pack("!HH", qtype, qclass)


def _rcode(reply: bytes) -> int:
    return struct.unpack("!H", reply[2:4])[0] & 0xF


def _answers(reply: bytes) -> int:
    return int(struct.unpack("!H", reply[6:8])[0])


def test_a_answers_a_stable_fake_address_per_name() -> None:
    names = netshim.Names()
    r1 = netshim.answer(_query("repo.maven.apache.org"), names)
    r2 = netshim.answer(_query("Repo.Maven.Apache.org"), names)
    r3 = netshim.answer(_query("pypi.org"), names)
    assert r1 is not None and r2 is not None and r3 is not None
    assert _rcode(r1) == 0 and _answers(r1) == 1 and r1[:2] == b"\x12\x34" and r1[2] & 0x80  # QR set, same id
    ip1, ip3 = socket.inet_ntoa(r1[-4:]), socket.inet_ntoa(r3[-4:])
    assert ip1 == socket.inet_ntoa(r2[-4:]) == "127.70.0.1" and ip3 == "127.70.0.2"
    assert struct.unpack("!I", r1[-10:-6])[0] == 60  # TTL
    assert names.name(ip1) == "repo.maven.apache.org"


def test_fake_addresses_skip_dot_0_and_dot_255() -> None:
    names = netshim.Names()
    ips = [names.address(f"h{i}.example") for i in range(300)]
    assert "127.70.0.255" not in ips and "127.70.1.0" not in ips and ips[254] == "127.70.1.1"
    assert all(ip is not None and ip.startswith("127.70.") for ip in ips)


@pytest.mark.parametrize("qtype", [28, 15, 16, 255])  # AAAA, MX, TXT, ANY
def test_every_other_type_is_noerror_without_an_answer(qtype: int) -> None:
    reply = netshim.answer(_query("example.com", qtype=qtype), netshim.Names())
    assert reply is not None and _rcode(reply) == 0 and _answers(reply) == 0


@pytest.mark.parametrize(
    "name", ["localhost", "x.localhost", "printer.invalid", "single", "bad_name.example", "-a.example",
             ".".join(["a" * 60] * 5)],
)  # fmt: skip
def test_nxdomain(name: str) -> None:
    reply = netshim.answer(_query(name), netshim.Names())
    assert reply is not None and _rcode(reply) == 3 and _answers(reply) == 0


@pytest.mark.parametrize(
    "msg",
    [
        struct.pack("!HHHHHH", 1, 0x8100, 1, 0, 0, 0) + b"\x00\x00\x01\x00\x01",  # a response, not a query
        struct.pack("!HHHHHH", 1, 0x0100, 2, 0, 0, 0) + b"\x00\x00\x01\x00\x01",  # two questions
        struct.pack("!HHHHHH", 1, 0x0100, 1, 0, 0, 0) + b"\xc0\x0c\x00\x01\x00\x01",  # compression
        struct.pack("!HHHHHH", 1, 0x0100, 1, 0, 0, 0) + b"\x05ab",  # truncated
        struct.pack("!HHHHHH", 1, 0x2100, 1, 0, 0, 0) + b"\x00\x00\x01\x00\x01",  # opcode 4
        _query("a" * 64 + ".example"),  # a 64-byte label is not a wire label (0x40 is a reserved type)
    ],
)
def test_formerr(msg: bytes) -> None:
    reply = netshim.answer(msg, netshim.Names())
    assert reply is not None and _rcode(reply) == 1


def test_qclass_other_than_in_and_short_messages() -> None:
    assert _rcode(netshim.answer(_query("example.com", qclass=3), netshim.Names()) or b"") == 1
    assert netshim.answer(b"\x00\x01", netshim.Names()) is None


def test_name_cap_is_servfail() -> None:
    names = netshim.Names(limit=2)
    assert netshim.answer(_query("a.example"), names) and netshim.answer(_query("b.example"), names)
    reply = netshim.answer(_query("c.example"), names)
    assert reply is not None and _rcode(reply) == 2
    again = netshim.answer(_query("a.example"), names)
    assert again is not None and _rcode(again) == 0  # known names keep answering


def test_udp_oversize_is_formerr() -> None:
    sent: list[bytes] = []
    proto = netshim.DnsUdp(netshim.Names())

    class T:
        def sendto(self, data: bytes, addr: Any) -> None:
            sent.append(data)

    proto.transport = T()  # type: ignore[assignment]
    proto.datagram_received(_query("example.com") + b"\x00" * 600, ("127.0.0.11", 5353))
    assert len(sent) == 1 and _rcode(sent[0]) == 1


# ------------------------------------------------------------------------------ tunnels
def test_http_head_rewrite() -> None:
    head = (b"GET /maven2/x.pom?a=1 HTTP/1.1\r\nHost: repo.maven.apache.org\r\nProxy-Authorization: x\r\n"
            b"Proxy-Connection: keep-alive\r\nConnection: keep-alive\r\nUser-Agent: java\r\n\r\n")  # fmt: skip
    out = netshim.rewrite_http_head(head, "repo.maven.apache.org")
    assert out == (b"GET http://repo.maven.apache.org/maven2/x.pom?a=1 HTTP/1.1\r\nHost: repo.maven.apache.org\r\n"
                   b"User-Agent: java\r\nConnection: close\r\n\r\n")  # fmt: skip
    for bad in (b"GET http://x/ HTTP/1.1\r\n\r\n", b"CONNECT x:443 HTTP/1.1\r\n\r\n", b"SSH-2.0-OpenSSH\r\n\r\n",
                b"GET / HTTP/2\r\n\r\n"):  # fmt: skip
        assert netshim.rewrite_http_head(bad, "x.example") is None


def test_proxy_address_from_the_environment() -> None:
    assert netshim.proxy_address({"HTTPS_PROXY": "http://proxy:3128"}) == ("proxy", 3128)
    assert netshim.proxy_address({}) == ("proxy", 3128)


class FakeProxy:
    """Records what the shim sends; answers CONNECT with ``status`` then echoes the tunnel."""

    def __init__(self, status: bytes = b"HTTP/1.1 200 Connection established\r\n\r\n") -> None:
        self.status = status
        self.received: list[bytes] = []

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head, rest = await netshim._read_head(reader, 65536)
        self.received.append(head)
        if head.startswith(b"CONNECT "):
            writer.write(self.status)
            await writer.drain()
            if not self.status.startswith(b"HTTP/1.1 200"):
                writer.close()
                return
            data = rest + await reader.read(65536)
            self.received.append(data)
            writer.write(b"echo:" + data)
        else:
            self.received.append(rest)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()


async def _run_tunnel(port: int, fake: FakeProxy, payload: bytes, bind: str = "127.70.0.1",
                      max_conns: int = 512) -> bytes:  # fmt: skip
    proxy = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
    pport = proxy.sockets[0].getsockname()[1]
    names = netshim.Names()
    assert names.address("repo.maven.apache.org") == "127.70.0.1"
    shim = netshim.Shim(names, ("127.0.0.1", pport), max_conns=max_conns)
    server = await asyncio.start_server(lambda r, w: shim.handle(r, w, port), bind, 0)
    sport = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection(bind, sport)
    writer.write(payload)
    await writer.drain()
    try:
        out = await asyncio.wait_for(reader.read(65536), timeout=5)
    except ConnectionResetError:  # the shim closed with our bytes unread: the kernel resets
        out = b""
    writer.close()
    server.close()
    proxy.close()
    return out


def test_443_becomes_connect_and_is_spliced() -> None:
    fake = FakeProxy()
    out = asyncio.run(_run_tunnel(443, fake, b"\x16\x03\x01tls-hello"))
    assert fake.received[0] == b"CONNECT repo.maven.apache.org:443 HTTP/1.1\r\nHost: repo.maven.apache.org:443\r\n\r\n"
    assert out == b"echo:\x16\x03\x01tls-hello"


def test_443_proxy_deny_closes_the_client() -> None:
    fake = FakeProxy(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
    assert asyncio.run(_run_tunnel(443, fake, b"hello")) == b""


def test_80_becomes_an_absolute_form_request() -> None:
    fake = FakeProxy()
    out = asyncio.run(_run_tunnel(80, fake, b"GET /x HTTP/1.1\r\nHost: repo.maven.apache.org\r\n\r\n"))
    assert fake.received[0].startswith(b"GET http://repo.maven.apache.org/x HTTP/1.1\r\n")
    assert b"Connection: close\r\n" in fake.received[0] and out.endswith(b"ok")


def test_80_non_http_is_closed() -> None:
    fake = FakeProxy()
    assert asyncio.run(_run_tunnel(80, fake, b"SSH-2.0-OpenSSH_9\r\n\r\n")) == b""
    assert fake.received == []


def test_unmapped_local_addresses_are_closed() -> None:
    """127.0.0.1 (an IP literal like 0x7f.1 parses to it) is never a mapped name: closed, nothing proxied."""
    fake = FakeProxy()
    assert asyncio.run(_run_tunnel(443, fake, b"hello", bind="127.0.0.1")) == b""
    assert fake.received == []


def test_connection_cap() -> None:
    fake = FakeProxy()
    assert asyncio.run(_run_tunnel(443, fake, b"x", max_conns=0)) == b""
    assert netshim.MAX_CONNS == 512 and netshim.IDLE_S == 600.0 and netshim.BUF == 64 * 1024


def test_bind_failure_is_reported(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*a: Any, **k: Any) -> dict[str, socket.socket]:
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(netshim, "bind_sockets", refuse)
    assert netshim.main([]) == 1
    assert "ossbot-netshim: cannot bind (Address already in use)" in capsys.readouterr().err


def test_netshim_is_stdlib_only() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(Path(netshim.__file__).read_text(encoding="utf-8"))
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not mods & {"ossbot", "pydantic", "httpx", "yaml"}
