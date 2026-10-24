"""The squid ``ossbot`` access-log format, per-job egress usage and floor classification (open-discovery design
§13.1, §13.2; build-env design §4.3, §4.5, ADR-0025).

Pure and stdlib-only (``ossbot.common`` depends on no component): sandboxd's egress meter, ``ossctl`` and the
floor-reach system test read it. The format is ``sandbox/proxy/squid.conf.j2``'s
``logformat ossbot %ts.%03tu %>a %>lp %Ss/%03>Hs %rm %ru %<a %>st %<st %tr %{ossbot_floor}note``, eleven
whitespace-separated tokens: time (``s.ms``), client address, listener port, ``result/status``, method, URL
(``host:port`` for CONNECT), server address, bytes received from the client (up), bytes sent to the client
(down), response time in ms, and the floor mark squid's marked deny lines set (``-`` when none; one of
``dst dst_loop name quiet literal code``). Rotated logs written before ADR-0025 have the first ten tokens only.
HTTPS content, paths and queries inside a CONNECT tunnel are not visible (no TLS interception), and a tunnel's
line is written when it closes.

A line counts for a job when its client address is in the job's /28, the listener port is the sandbox
port (3128) and its time is at or after the job's start minus one second. Allowed and denied requests both
count toward the distinct hosts (DNS-label exfiltration through denied names shows up); denies are also
counted on their own.

``floor_class`` decides, for one line of a metered job, whether it was an attempt to reach a floor (§4.5):
``hold`` classes stop the job and hold the contribution in every mode; ``note`` classes (names and literals
that can only reach the proxy's own loopback, the HashiCorp checkpoint beacon, a repo-code-denied name in a
base pool) are denied and noted. ``python -m ossbot.common.egress_log classify`` prints the class of every
line on stdin (the floor-reach system test).
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

SANDBOX_PORT = 3128  # ctl/render.py SANDBOX_PORT: the listener of every sandbox pool
TOKENS = 11
OLD_TOKENS = 10  # rotated logs written before the floor mark (ADR-0025)
UNKNOWN_HOST = "?"
TOP_HOSTS = 10
FLOOR_MARKS = frozenset({"dst", "dst_loop", "name", "quiet", "literal", "code"})

# The address floors no egress mode relaxes (build-env design §4.1; DESIGN §11.5). ctl/render.py unions them
# with the snapshot's always_denied.cidrs (config can only add), the kernel floor of OSSBOT-FWD uses the IPv4
# rows, and the meter classifies squid's server addresses against them.
REQUIRED_DENIED_CIDRS = (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "127.0.0.0/8",
    "0.0.0.0/8", "198.18.0.0/15", "192.0.0.0/24", "224.0.0.0/4", "240.0.0.0/4",
    "::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8", "64:ff9b::/96", "2002::/16",
)  # fmt: skip
# Floors that can only reach the proxy's own netns (squid's forwarding-loop detection; nothing else listens):
# a name or literal resolving there is denied and noted, never held (§4.3 denied_dst_loop).
LOOP_CIDRS = ("127.0.0.0/8", "0.0.0.0/8", "::1/128", "::/128")
IPV4_FLOOR_CIDRS = tuple(c for c in REQUIRED_DENIED_CIDRS if ":" not in c)

FloorPolicy = Literal["hold", "note"]
FloorClassName = Literal["dst", "loopback", "name", "code", "literal", "quiet", "host", "proxy", "pub"]
JobNet = Literal["web", "base"]
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


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
    floor: str | None = None  # the squid floor mark (ADR-0025); None for "-" and for 10-token lines

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
    if len(parts) == OLD_TOKENS:
        mark: str | None = None
    elif len(parts) == TOKENS:
        raw_mark = parts.pop()
        if raw_mark != "-" and raw_mark not in FLOOR_MARKS:
            return None
        mark = None if raw_mark == "-" else raw_mark
    else:
        return None
    ts, client, port, result_status, method, url, server, up, down, ms = parts
    result, sep, status = result_status.partition("/")
    try:
        entry = Entry(ts=float(ts), client=client, port=int(port), result=result, status=int(status) if sep else 0,
                      method=method, host=host_of(method, url), server=server, up=_int(up), down=_int(down),
                      ms=_int(ms), floor=mark)  # fmt: skip
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


# ------------------------------------------------------------------ floor classification (§4.5)
def _unmapped(ip: IPAddress) -> IPAddress:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def parse_ip(value: str) -> IPAddress | None:
    """A literal IP address in any form squid's ``ip_literal`` ACL denies: dotted, short, decimal, octal and
    hex IPv4 (``socket.inet_aton``: ``0x7f.1``, ``2130706433``, ``017700000001``), IPv6 with or without brackets
    (an IPv4-mapped IPv6 address becomes its IPv4 address). None when it isn't one."""
    text = value.strip().lower().rstrip(".")
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text or text == UNKNOWN_HOST:
        return None
    if ":" in text:
        try:
            return _unmapped(ipaddress.IPv6Address(text.split("%", 1)[0]))
        except ValueError:
            return None
    if not all(part and (part.isdigit() or part.startswith("0x")) for part in text.split(".")):
        return None  # inet_aton also accepts trailing garbage on some libcs; only digit/hex parts here
    try:
        return ipaddress.IPv4Address(socket.inet_aton(text))
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class Floors:
    """The address floors one classification uses: ``cidrs`` the non-loopback ranges plus host /32s, ``loop``
    the loopback ranges, ``public`` the learned public addresses of the owner (§4.7)."""

    cidrs: tuple[IPNetwork, ...]
    loop: tuple[IPNetwork, ...]
    public: tuple[IPNetwork, ...] = ()

    @classmethod
    def of(cls, denied_cidrs: Iterable[str], host_cidrs: Iterable[str] = (), public: Iterable[str] = ()) -> Floors:
        loop = {ipaddress.ip_network(c) for c in LOOP_CIDRS}
        nets = [ipaddress.ip_network(c) for c in dict.fromkeys([*REQUIRED_DENIED_CIDRS, *denied_cidrs])]
        hosts = [ipaddress.ip_network(c) for c in host_cidrs]
        return cls(
            cidrs=tuple(n for n in [*nets, *hosts] if n not in loop),
            loop=tuple(sorted(loop, key=str)),
            public=tuple(ipaddress.ip_network(c) for c in public),
        )

    @staticmethod
    def _in(ip: IPAddress, nets: Sequence[IPNetwork]) -> bool:
        return any(ip.version == n.version and ip in n for n in nets)

    def is_public(self, ip: IPAddress) -> bool:
        return self._in(ip, self.public)

    def is_loop(self, ip: IPAddress) -> bool:
        return self._in(ip, self.loop)

    def is_floor(self, ip: IPAddress) -> bool:
        """A non-loopback floor range, a host /32 or the public floor."""
        return self._in(ip, self.cidrs) or self.is_public(ip)


@dataclass(frozen=True)
class FloorHit:
    """One floor attempt: the class (the card text), the policy, the target (a host name or the literal) and the
    destination address (an IP literal or ``-``). Public-floor values never leave sandboxd (§4.7 privacy):
    ``public`` says the hit concerns the owner's public address, so the sender replaces both values."""

    cls: FloorClassName
    policy: FloorPolicy
    target: str
    dst: str = "-"
    public: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"class": self.cls, "policy": self.policy, "target": self.target, "dst": self.dst,
                "public": self.public}  # fmt: skip


def floor_class(entry: Entry, floors: Floors, net: JobNet) -> FloorHit | None:
    """The §4.5 rows for one squid line of a metered job (pure). ``net`` is the job's network: ``web`` (base +
    web_public) or ``base``; it only matters for the ``code`` mark."""
    server = parse_ip(entry.server) if entry.server not in ("-", "") else None
    if server is not None and floors.is_floor(server):  # supplementary (review 2 M2): squid printed a floor
        public = floors.is_public(server)
        return FloorHit("pub" if public else "dst", "hold", entry.host, str(server), public)
    mark = entry.floor
    if mark == "dst":
        return FloorHit("dst", "hold", entry.host)
    if mark == "dst_loop":
        return FloorHit("loopback", "note", entry.host)
    if mark == "name":
        return FloorHit("name", "hold", entry.host)
    if mark == "code":
        return FloorHit("code", "hold" if net == "web" else "note", entry.host)
    if mark == "quiet":
        return FloorHit("quiet", "note", entry.host)
    if mark == "literal":
        ip = parse_ip(entry.host)
        if ip is None:
            return None  # not decodable: squid denied it as a literal; it reached nothing
        if floors.is_loop(ip):
            return FloorHit("loopback", "note", entry.host, str(ip))
        if floors.is_floor(ip):
            return FloorHit("literal", "hold", entry.host, str(ip), floors.is_public(ip))
        return None  # a public address: an ordinary deny (counts as a host, as today)
    return None


# ----------------------------------------------------------------------------------- cli
def _read_public(path: Path | None) -> list[str]:
    if path is None:
        return []
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if value and not value.startswith("#"):
            out.append(str(ipaddress.ip_network(value, strict=True)))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ossbot.common.egress_log")
    sub = parser.add_subparsers(dest="what", required=True)
    cl = sub.add_parser("classify", help="print the floor class and policy of every squid line on stdin")
    cl.add_argument("--net", choices=("web", "base"), required=True)
    cl.add_argument("--cidr", action="append", default=[], help="an extra floor range (default: the built-ins)")
    cl.add_argument("--host", action="append", default=[], help="a host /32 floor")
    cl.add_argument("--public-file", type=Path, default=None, help="public.txt of the public-address floor")
    args = parser.parse_args(argv)
    floors = Floors.of(args.cidr, args.host, _read_public(args.public_file))
    for raw in sys.stdin:
        if not raw.strip():
            continue
        entry = parse_line(raw)
        if entry is None:
            print(json.dumps({"malformed": True}))
            continue
        hit = floor_class(entry, floors, args.net)
        print(json.dumps({"host": entry.host, "mark": entry.floor, "client": entry.client,
                          **(hit.as_dict() if hit else {"class": None, "policy": None})}))  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
