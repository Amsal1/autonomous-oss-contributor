"""The public-address floor's files (build-env design §4.7, ADR-0025): the owner's own public address(es)
are a floor in squid and in the kernel; ``ossbot-pubip`` (root) learns them, these files carry them.

``/etc/ossbot/proxy/floor/`` (``root:root 0755``; files ``0644``, written to a temp name and renamed):

- ``state.json``: ``{"schema": 1, "known": bool, "last_v4_success": ts, "v4": [{"ip", "first_seen",
  "last_seen"}], "v6": [...], "generation": n}``. Readers (sandboxd admission and meter, ossctl, doctor, the
  floor test) parse it with ``PublicFloorState`` and recompute ``known`` from ``last_v4_success`` (≤ 300 s)
  instead of trusting the stored flag; a missing or invalid file reads as unknown (fail closed).
- ``public.txt``: the sentinel ``240.0.0.1/32`` (already a floor, so the file is never empty) and one ``/32``
  or ``/128`` per kept entry (squid's second ``denied_dst`` line, the meter, ``OSSBOT-PUBFLOOR``).
- ``code-web.txt``: the repo-code pool ``10.213.0.0/18`` while known, else the sentinel ``255.255.255.255/32``
  (squid's ``code_web_ok``: repo code has no public web while the address is unknown).
- ``.lock``: the root-only lock (``ossbot.common.floor_lock``).

The address is personal data: nothing here logs a value (counts only).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

log = logging.getLogger(__name__)

FLOOR_DIR = Path("/etc/ossbot/proxy/floor")
STATE_FILE = "state.json"
PUBLIC_FILE = "public.txt"
CODE_WEB_FILE = "code-web.txt"
PUBLIC_SENTINEL = "240.0.0.1/32"  # 240.0.0.0/4 is already a floor; keeps the file ACL non-empty
CODE_WEB_SENTINEL = "255.255.255.255/32"  # no sandbox has this source: code_web_ok matches nothing
CODE_WEB_POOL = "10.213.0.0/18"  # pool_fetch (ctl/render.py SQUID_POOLS): fetch, validate, agent-exec
KNOWN_S = 300.0  # known = the last valid IPv4 answer is at most this old
KEEP_S = 24 * 3600.0  # an entry stays floored this long after it was last seen
MAX_PER_FAMILY = 8
MAX_PUBLIC_LINES = 1 + 2 * MAX_PER_FAMILY  # the sentinel + 8 per family


def _global_ip(value: str, version: int) -> str:
    ip = ipaddress.ip_address(value)
    if ip.version != version or not ip.is_global:
        raise ValueError("not a global address of the family")
    return str(ip)


class FloorEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ip: Annotated[str, Field(min_length=2, max_length=45)]
    first_seen: Annotated[float, Field(ge=0)]
    last_seen: Annotated[float, Field(ge=0)]


class PublicFloorState(BaseModel):
    """``state.json`` (schema 1). ``known`` is stored for humans; readers recompute it (``is_known``)."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Annotated[Literal[1], Field(alias="schema")] = 1
    known: bool = False
    last_v4_success: Annotated[float, Field(ge=0)] = 0.0
    v4: Annotated[list[FloorEntry], Field(max_length=MAX_PER_FAMILY)] = Field(default_factory=list)
    v6: Annotated[list[FloorEntry], Field(max_length=MAX_PER_FAMILY)] = Field(default_factory=list)
    generation: Annotated[int, Field(ge=0)] = 0

    @field_validator("v4")
    @classmethod
    def _v4_global(cls, entries: list[FloorEntry]) -> list[FloorEntry]:
        for e in entries:
            _global_ip(e.ip, 4)
        return entries

    @field_validator("v6")
    @classmethod
    def _v6_global(cls, entries: list[FloorEntry]) -> list[FloorEntry]:
        for e in entries:
            _global_ip(e.ip, 6)
        return entries

    def is_known(self, now: float) -> bool:
        return bool(self.v4) and self.last_v4_success > 0 and now - self.last_v4_success <= KNOWN_S

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json", by_alias=True), sort_keys=True) + "\n"


@dataclass(frozen=True)
class FloorStatus:
    """What a reader may show: ``known`` (recomputed), ``since`` (when it became unknown: the last good
    answer plus ``KNOWN_S``; None while known or when it never was), ``entries`` (a count, never a value)."""

    known: bool
    since: float | None
    entries: int
    generation: int
    valid: bool  # False: the file was missing or invalid (read as unknown)

    def as_dict(self) -> dict[str, object]:
        return {"known": self.known, "since": self.since, "entries": self.entries}


def load_state(path: Path) -> PublicFloorState | None:
    """The parsed ``state.json``, or None when it is missing or invalid (WARNING ``public_floor_state_invalid``
    for an invalid one; the value is never logged)."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("public floor state unreadable (%s)", type(exc).__name__,
                    extra={"event": "public_floor_state_invalid"})  # fmt: skip
        return None
    try:
        if len(raw) > 64 * 1024:
            raise ValueError("oversize")
        return PublicFloorState.model_validate(json.loads(raw.decode("utf-8")))
    except (ValueError, ValidationError, UnicodeDecodeError) as exc:
        log.warning("public floor state invalid (%s); read as unknown", type(exc).__name__,
                    extra={"event": "public_floor_state_invalid"})  # fmt: skip
        return None


def read_state(floor_dir: Path = FLOOR_DIR, now: float | None = None) -> FloorStatus:
    state = load_state(floor_dir / STATE_FILE)
    t = time.time() if now is None else now
    if state is None:
        return FloorStatus(False, None, 0, 0, False)
    known = state.is_known(t)
    since = None if known or state.last_v4_success <= 0 else state.last_v4_success + KNOWN_S
    return FloorStatus(known, since, len(state.v4) + len(state.v6), state.generation, True)


def parse_public_text(text: str) -> list[str]:
    """``public.txt``: one ``/32`` or ``/128`` network per line, at most ``MAX_PUBLIC_LINES``, the sentinel
    present. Returns the networks in file order without the sentinel; raises ValueError otherwise."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) > MAX_PUBLIC_LINES:
        raise ValueError(f"public floor file has {len(lines)} lines (max {MAX_PUBLIC_LINES})")
    out: list[str] = []
    for line in lines:
        net = ipaddress.ip_network(line, strict=True)
        if net.prefixlen != net.max_prefixlen:
            raise ValueError("public floor entries must be single addresses")
        out.append(str(net))
    if PUBLIC_SENTINEL not in out:
        raise ValueError("public floor file lacks its sentinel")
    return [n for n in out if n != PUBLIC_SENTINEL]


def read_public(floor_dir: Path = FLOOR_DIR) -> list[str]:
    """The public-floor networks of ``public.txt`` (file order, sentinel dropped). A missing file is no entry
    (the fail-closed ``code-web.txt`` sentinel withholds the web then); an invalid one raises ValueError."""
    try:
        text = (floor_dir / PUBLIC_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return parse_public_text(text)


def public_text(entries: list[str]) -> str:
    return "".join(f"{e}\n" for e in [PUBLIC_SENTINEL, *entries])


def code_web_text(known: bool) -> str:
    return f"{CODE_WEB_POOL if known else CODE_WEB_SENTINEL}\n"
