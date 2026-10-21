"""The squid ``ossbot`` access-log format and per-job egress usage (open-discovery design §13.1, §13.2).

Pure and stdlib-only (``ossbot.common`` depends on no component): sandboxd's egress meter and ``ossctl``
read it. The format is ``sandbox/proxy/squid.conf.j2``'s
``logformat ossbot %ts.%03tu %>a %>lp %Ss/%03>Hs %rm %ru %<a %>st %<st %tr``, ten whitespace-separated
tokens: time (``s.ms``), client address, listener port, ``result/status``, method, URL (``host:port`` for
CONNECT), server address, bytes received from the client (up), bytes sent to the client (down), response
time in ms. HTTPS content, paths and queries inside a CONNECT tunnel are not visible (no TLS interception),
and a tunnel's line is written when it closes.

A line counts for a job when its client address is in the job's /28, the listener port is the sandbox
port (3128) and its time is at or after the job's start minus one second. Allowed and denied requests both
count toward the distinct hosts (DNS-label exfiltration through denied names shows up); denies are also
counted on their own.
"""

from __future__ import annotations

import ipaddress
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

SANDBOX_PORT = 3128  # ctl/render.py SANDBOX_PORT: the listener of every sandbox pool
TOKENS = 10
UNKNOWN_HOST = "?"
TOP_HOSTS = 10


@dataclass(frozen=True)
class Entry:
    ts: float
    client: str
    port: int
    result: str  # squid's %Ss, e.g. TCP_TUNNEL, TCP_DENIED
    status: int
    method: str
    host: str  # lower-cased, without the port; "?" when the URL has no readable host
    server: str
    up: int  # %>st: bytes received from the client
    down: int  # %<st: bytes sent to the client
    ms: int

    @property
    def denied(self) -> bool:
        return "DENIED" in self.result


def host_of(method: str, url: str) -> str:
    """CONNECT ``host:port`` -> host; any other URL -> its hostname; lower-cased, ``?`` when unreadable."""
    try:
        target = urlsplit(f"//{url}" if method == "CONNECT" else url).hostname
    except ValueError:
        target = None
    return (target or UNKNOWN_HOST).lower()


def _int(value: str) -> int:
    return 0 if value == "-" else int(value)


def parse_line(line: str) -> Entry | None:
    """One log line, or None when it isn't the ``ossbot`` format (the caller counts it as malformed)."""
    parts = line.split()
    if len(parts) != TOKENS:
        return None
    ts, client, port, result_status, method, url, server, up, down, ms = parts
    result, sep, status = result_status.partition("/")
    try:
        entry = Entry(ts=float(ts), client=client, port=int(port), result=result, status=int(status) if sep else 0,
                      method=method, host=host_of(method, url), server=server, up=_int(up), down=_int(down),
                      ms=_int(ms))  # fmt: skip
        ipaddress.IPv4Address(client)
    except ValueError:
        return None
    if entry.up < 0 or entry.down < 0:
        return None
    return entry


@dataclass(frozen=True)
class JobWindow:
    subnet: ipaddress.IPv4Network
    start_ts: float
    port: int = SANDBOX_PORT

    @classmethod
    def of(cls, subnet: str, start_ts: float) -> JobWindow:
        return cls(ipaddress.IPv4Network(subnet), start_ts)

    def matches(self, e: Entry) -> bool:
        return e.port == self.port and e.ts >= self.start_ts - 1 and ipaddress.IPv4Address(e.client) in self.subnet


@dataclass
class Usage:
    hosts: Counter[str] = field(default_factory=Counter)
    up: int = 0
    down: int = 0
    denied: int = 0
    requests: int = 0

    def add(self, e: Entry) -> None:
        self.hosts[e.host] += 1
        self.up += e.up
        self.down += e.down
        self.denied += int(e.denied)
        self.requests += 1

    def top_hosts(self, n: int = TOP_HOSTS) -> list[str]:
        """By request count, then name (deterministic)."""
        return [h for h, _ in sorted(self.hosts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]

    def as_dict(self) -> dict[str, Any]:
        return {"hosts": len(self.hosts), "up_bytes": self.up, "down_bytes": self.down, "denied": self.denied,
                "requests": self.requests, "top_hosts": self.top_hosts()}  # fmt: skip


def aggregate(lines: Iterable[str], window: JobWindow) -> tuple[Usage, int]:
    """(usage of the job, malformed line count) over a batch of lines."""
    usage, malformed = Usage(), 0
    for line in lines:
        if not line.strip():
            continue
        e = parse_line(line)
        if e is None:
            malformed += 1
        elif window.matches(e):
            usage.add(e)
    return usage, malformed
