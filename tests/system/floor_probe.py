"""Stdlib probes for the floor-reach test, run inside a repo-code container (build-env design §4.6 step 3).

    python3 -I floor_probe.py GW LIVE_GW ETH0 NS BCAST

Prints one ``key=value`` line per probe. Every attempt has a short timeout and runs concurrently, so the
container finishes in seconds; the host side decides what each value must be. Values: ``open`` / ``reply`` /
``answer`` (the floor was reached: always a failure), ``timeout``, ``eperm``, ``errnoN``, ``none``.
"""

from __future__ import annotations

import concurrent.futures
import os
import socket
import sys
from collections.abc import Callable
from typing import Any

TIMEOUT = 5.0
ICMP_TIMEOUT = 3.0
QUERY = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"


def outcome(fn: Callable[[], str]) -> str:
    try:
        return str(fn())
    except TimeoutError:
        return "timeout"
    except PermissionError:
        return "eperm"
    except OSError as exc:
        return f"errno{exc.errno}" if exc.errno else type(exc).__name__


def tcp(host: str, port: int, family: int = socket.AF_INET) -> str:
    def go() -> str:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT)
            s.connect((host, port))
            return "open"

    return outcome(go)


def udp(host: str, port: int, family: int = socket.AF_INET, payload: bytes = QUERY, bcast: bool = False) -> str:
    def go() -> str:
        with socket.socket(family, socket.SOCK_DGRAM) as s:
            if bcast:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.settimeout(ICMP_TIMEOUT)
            s.sendto(payload, (host, port))
            s.recv(512)
            return "answer"

    return outcome(go)


def icmp(host: str) -> str:
    def go() -> str:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP) as s:
            s.settimeout(ICMP_TIMEOUT)
            s.sendto(b"\x08\x00\x00\x00\x00\x01\x00\x01ossbot-floor", (host, 0))
            s.recv(64)
            return "reply"

    return outcome(go)


def raw(family: int, kind: int, proto: int) -> str:
    def go() -> str:
        socket.socket(family, kind, proto).close()
        return "open"

    return outcome(go)


def v6_addresses() -> str:
    """Global or ULA IPv6 addresses (link-local fe80::/10 and ::1 don't count)."""
    try:
        lines = open("/proc/net/if_inet6", encoding="ascii").read().splitlines()  # noqa: SIM115
    except OSError:
        return "none"
    out = []
    for line in lines:
        addr, scope = line.split()[0], line.split()[3]
        if scope == "00" and not addr.startswith("fe80"):  # scope 00 = global (ULA included)
            out.append(addr)
    return ",".join(out) or "none"


def interfaces() -> str:
    try:
        return ",".join(sorted(os.listdir("/sys/class/net")))
    except OSError:
        return "?"


def main(gw: str, live_gw: str, eth0: str, ns: str, bcast: str) -> int:
    probes: dict[str, tuple[Any, ...]] = {}
    for port in (22, 53, 80, 443, 2375, 2376, 3128, 5432, 8642):
        probes[f"gw_tcp_{port}"] = (tcp, gw, port)
    for port in (53, 5353):
        probes[f"gw_udp_{port}"] = (udp, gw, port)
    for host in ("10.212.0.1", "10.212.0.2"):
        for port in (3140, 3141, 3150):
            probes[f"proxyhost_{host}_{port}"] = (tcp, host, port)
    for name, host in (("livegw", live_gw), ("eth0", eth0), ("ns", ns)):
        for port in (53, 80, 443, 445, 3389, 5985):
            probes[f"{name}_tcp_{port}"] = (tcp, host, port)
    for host, port in (("169.254.169.254", 80), ("100.100.100.200", 80), ("10.0.0.1", 80), ("172.16.0.1", 80),
                       ("192.168.0.1", 80), ("100.64.0.1", 80), ("1.1.1.1", 443)):  # fmt: skip
        probes[f"direct_{host}_{port}"] = (tcp, host, port)
    probes["udp_8.8.8.8_53"] = (udp, "8.8.8.8", 53)
    probes["udp_ns_53"] = (udp, ns, 53)
    probes["udp_mdns"] = (udp, "224.0.0.251", 5353)
    probes["udp_bcast"] = (lambda h, p: udp(h, p, bcast=True), bcast, 9)
    probes["v6_fd00_443"] = (tcp, "fd00::1", 443, socket.AF_INET6)
    probes["v6_public_443"] = (tcp, "2606:4700:4700::1111", 443, socket.AF_INET6)
    probes["v6_ll_443"] = (tcp, "fe80::1%eth0", 443, socket.AF_INET6)
    probes["v6_mcast"] = (lambda h, p: udp(h, p, socket.AF_INET6, b"ossbot"), "ff02::1", 5353)
    probes["v6_mcast_eth0"] = (lambda h, p: udp(h, p, socket.AF_INET6, b"ossbot"), "ff02::1%eth0", 5353)
    for name, host in (("gw", gw), ("livegw", live_gw), ("rfc1918", "10.0.0.1"), ("meta", "169.254.169.254")):
        probes[f"icmp_{name}"] = (icmp, host)
    probes["raw_icmp"] = (raw, socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
    probes["raw_tcp"] = (raw, socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_TCP)
    probes["raw_packet"] = (raw, socket.AF_PACKET, socket.SOCK_RAW, 0)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(probes)) as pool:
        futures = {k: pool.submit(fn, *a) for k, (fn, *a) in probes.items()}
        results = {k: f.result() for k, f in futures.items()}
    results["v6_addresses"] = v6_addresses()
    results["interfaces"] = interfaces()
    for key in sorted(results):
        print(f"{key}={results[key]}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 6:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(*sys.argv[1:]))
