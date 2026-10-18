"""Squid access-log parsing (native format, DESIGN §11.2): which hosts a job's network reached.

Used to record the hosts a research job contacted (web tools client- vs server-side, §23 items 6
and 29) and in the stage evidence. Lines that do not parse are skipped, never trusted.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

LINE_RE = re.compile(
    r"^(?P<ts>\d+\.\d+)\s+(?P<elapsed>\d+)\s+(?P<client>[0-9a-fA-F.:]+)\s+(?P<code>[A-Z_]+)/(?P<status>\d{3})\s+"
    r"(?P<bytes>\d+)\s+(?P<method>[A-Z]+)\s+(?P<url>\S+)"
)


@dataclass(frozen=True)
class Access:
    ts: float
    client: str
    code: str
    status: int
    method: str
    host: str

    @property
    def denied(self) -> bool:
        return self.code.startswith("TCP_DENIED") or self.status == 403


def _host(method: str, url: str) -> str:
    if method == "CONNECT":
        return url.rsplit(":", 1)[0].lower()
    m = re.match(r"^[a-z]+://([^/:]+)", url, re.IGNORECASE)
    return m.group(1).lower() if m else url[:200].lower()


def parse(lines: Iterable[str]) -> list[Access]:
    out: list[Access] = []
    for line in lines:
        m = LINE_RE.match(line.strip())
        if m is None:
            continue
        out.append(Access(float(m["ts"]), m["client"], m["code"], int(m["status"]), m["method"],
                          _host(m["method"], m["url"])))  # fmt: skip
    return out


def hosts_by_client(entries: Iterable[Access], clients: set[str] | None = None) -> dict[str, Counter[str]]:
    out: dict[str, Counter[str]] = {}
    for e in entries:
        if clients is not None and e.client not in clients:
            continue
        out.setdefault(e.client, Counter())[e.host] += 1
    return out
