"""ossbot-pubip (build-env design §4.7, ADR-0025): learns the owner's public address(es) and keeps the
public-address floor current, in squid and in the kernel.

A root oneshot (``systemd/ossbot-pubip.service``, every 60 s from ``ossbot-pubip.timer``), run as
``python -I -m ossbot.ctl.pubip``. It is root's own lookup from the distro's host netns, never from a
sandbox, and decides nothing beyond "which addresses are the owner's":

1. Ask two independent providers (``https://1.1.1.1/cdn-cgi/trace``: an ``ip=`` line; and
   ``https://checkip.amazonaws.com/``: the address alone) with curl (argv lists, no proxy, https only, TLS
   1.2+, 10 s, ≤ 4096 bytes), plus one IPv6 attempt (normally fails in WSL NAT mode). An answer must parse
   as a global address of the family asked for; anything else is a failed lookup for that provider
   (WARNING ``pubip_bad_answer``; the value is never logged).
2. Union, never a vote: every valid answer is kept for 24 h after it was last seen, at most 8 per family
   (oldest out first). ``known`` = the last valid IPv4 answer is at most 300 s old.
3. Under the EXCLUSIVE root-only floor lock (C-M1, ``ossbot.common.floor_lock``): if neither ``public.txt``
   nor ``code-web.txt`` changes, only ``state.json`` (timestamps) is rewritten. Otherwise the kernel first,
   make-before-break (``iptables -A OSSBOT-PUBFLOOR`` the rules of every added address, then ``-D`` those of
   every expired one: the chain always holds the union), then ``public.txt``, ``code-web.txt`` and
   ``state.json`` (temp + rename, 0644), then ``docker kill --signal=HUP ossbot-proxy`` (squid's reconfigure
   re-reads the directory-bound ACL files). Any iptables error: exit 1 with the three files untouched (the
   next run retries; a partial addition is extra drops, which ``--check`` reports as
   ``firewall_drift:public_floor`` if it persists).

Imports only ``ossbot.common`` and ``ossbot.ctl.render`` (import-linter contract). Logs, audit lines, cards
and digests never carry the address (counts and "your public address" only).
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ossbot.common.floor_lock import WRITE_TIMEOUT_S, FloorLockError, floor_lock
from ossbot.common.public_floor import (
    CODE_WEB_FILE,
    FLOOR_DIR,
    KEEP_S,
    MAX_PER_FAMILY,
    PUBLIC_FILE,
    STATE_FILE,
    FloorEntry,
    PublicFloorState,
    code_web_text,
    load_state,
    parse_public_text,
    public_text,
)
from ossbot.ctl.render import PUBFLOOR_CHAIN, floor_block

log = logging.getLogger("ossbot.pubip")

CURL = "/usr/bin/curl"
DOCKER = "/usr/bin/docker"
PROXY_CONTAINER = "ossbot-proxy"
CURL_OPTS = ("-sS", "--noproxy", "*", "--proto", "=https", "--tlsv1.2", "--max-time", "10", "--max-filesize",
             "4096", "-A", "ossbot-pubip")  # fmt: skip
MAX_BODY = 4096
_TRACE_IP_RE = re.compile(r"^ip=(\S{1,45})$", re.MULTILINE)

Ipt = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Provider:
    name: str
    url: str
    family: int
    trace: bool  # Cloudflare's key=value trace (an ip= line); else the body is the address alone

    def argv(self) -> list[str]:
        return [CURL, *(["-6"] if self.family == 6 else []), *CURL_OPTS, self.url]


PROVIDERS = (
    Provider("cloudflare", "https://1.1.1.1/cdn-cgi/trace", 4, True),
    Provider("aws", "https://checkip.amazonaws.com/", 4, False),
    Provider("cloudflare-v6", "https://[2606:4700:4700::1111]/cdn-cgi/trace", 6, True),
)


def parse_answer(body: bytes, provider: Provider) -> str | None:
    """The validated address of one answer, or None (never logs the value)."""
    if len(body) > MAX_BODY:
        return None
    try:
        text = body.decode("ascii")
    except UnicodeDecodeError:
        return None
    if provider.trace:
        found = _TRACE_IP_RE.findall(text)
        if len(found) != 1:
            return None
        value = found[0]
    else:
        value = text.strip()
        if len(value) > 64:
            return None
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    if ip.version != provider.family or not ip.is_global:
        return None  # a private or CGNAT answer adds nothing: those ranges are static floors
    return str(ip)


def lookup(provider: Provider) -> str | None:
    try:
        proc = subprocess.run(provider.argv(), capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("public address lookup via %s failed (%s)", provider.name, type(exc).__name__,
                    extra={"event": "pubip_lookup_failed"})  # fmt: skip
        return None
    if proc.returncode != 0:
        if provider.family == 4:  # the IPv6 attempt normally fails (no IPv6 route in WSL NAT mode)
            log.warning("public address lookup via %s failed (curl exit %s)", provider.name, proc.returncode,
                        extra={"event": "pubip_lookup_failed"})  # fmt: skip
        return None
    ip = parse_answer(proc.stdout, provider)
    if ip is None:
        log.warning("public address answer of %s rejected (not a single global address of the family)",
                    provider.name, extra={"event": "pubip_bad_answer"})  # fmt: skip
    return ip


def lookup_all(providers: Sequence[Provider] = PROVIDERS) -> tuple[set[str], set[str]]:
    v4: set[str] = set()
    v6: set[str] = set()
    for p in providers:
        ip = lookup(p)
        if ip is not None:
            (v4 if p.family == 4 else v6).add(ip)
    return v4, v6


def merge(entries: Sequence[FloorEntry], answers: Iterable[str], now: float) -> list[FloorEntry]:
    """Keep the order (it is the kernel chain's order), refresh seen entries, append new ones, drop entries
    not seen for 24 h, then cap at 8 (the least recently seen out first)."""
    out = list(entries)
    index = {e.ip: i for i, e in enumerate(out)}
    for ip in sorted(set(answers)):
        if ip in index:
            out[index[ip]] = FloorEntry(ip=ip, first_seen=out[index[ip]].first_seen, last_seen=now)
        else:
            index[ip] = len(out)
            out.append(FloorEntry(ip=ip, first_seen=now, last_seen=now))
    out = [e for e in out if now - e.last_seen <= KEEP_S]
    while len(out) > MAX_PER_FAMILY:
        oldest = min(range(len(out)), key=lambda i: (out[i].last_seen, i))
        del out[oldest]
    return out


def next_state(old: PublicFloorState | None, v4: set[str], v6: set[str], now: float) -> PublicFloorState:
    base = old or PublicFloorState()
    last = now if v4 else base.last_v4_success
    entries4, entries6 = merge(base.v4, v4, now), merge(base.v6, v6, now)
    draft = PublicFloorState(known=False, last_v4_success=last, v4=entries4, v6=entries6,
                             generation=base.generation)  # fmt: skip
    return draft.model_copy(update={"known": draft.is_known(now)})


def entries_of(state: PublicFloorState) -> list[str]:
    """public.txt's entries (file order: IPv4 in state order, then IPv6)."""
    return [f"{e.ip}/32" for e in state.v4] + [f"{e.ip}/128" for e in state.v6]


def write_file(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def ipt_default(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["iptables", "-w", *args], capture_output=True, text=True, check=False)  # noqa: S607


class KernelError(RuntimeError):
    pass


def kernel_update(old: Sequence[str], new: Sequence[str], ipt: Ipt) -> None:
    """Make-before-break on OSSBOT-PUBFLOOR (IPv4 only): add the rules of every new address first, then
    delete those of every address no longer kept; idempotent (``-C`` before ``-A``/``-D``)."""
    old4, new4 = [n for n in old if ":" not in n], [n for n in new if ":" not in n]
    for net in [n for n in new4 if n not in old4]:
        for rule in floor_block(net):
            if ipt("-t", "filter", "-C", PUBFLOOR_CHAIN, *rule).returncode != 0:
                done = ipt("-t", "filter", "-A", PUBFLOOR_CHAIN, *rule)
                if done.returncode != 0:
                    raise KernelError(f"iptables -A {PUBFLOOR_CHAIN} failed ({done.returncode})")
    for net in [n for n in old4 if n not in new4]:
        for rule in floor_block(net):
            for _ in range(8):  # a duplicate of the rule (a hand edit) goes too, never loops forever
                if ipt("-t", "filter", "-C", PUBFLOOR_CHAIN, *rule).returncode != 0:
                    break
                done = ipt("-t", "filter", "-D", PUBFLOOR_CHAIN, *rule)
                if done.returncode != 0:
                    raise KernelError(f"iptables -D {PUBFLOOR_CHAIN} failed ({done.returncode})")


def hup_proxy() -> None:
    try:
        proc = subprocess.run([DOCKER, "kill", "--signal=HUP", PROXY_CONTAINER], capture_output=True, timeout=30,
                              check=False)  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("SIGHUP to %s failed (%s); the files are in place for its next start", PROXY_CONTAINER,
                    type(exc).__name__, extra={"event": "pubip_hup_failed"})  # fmt: skip
        return
    if proc.returncode != 0:
        log.warning("SIGHUP to %s failed (docker exit %s); the files are in place for its next start",
                    PROXY_CONTAINER, proc.returncode, extra={"event": "pubip_hup_failed"})  # fmt: skip


@dataclass(frozen=True)
class Outcome:
    changed: bool
    known: bool
    v4: int
    v6: int


def update(
    floor_dir: Path,
    v4: set[str],
    v6: set[str],
    now: float,
    *,
    ipt: Ipt = ipt_default,
    hup: Callable[[], None] = hup_proxy,
    lock_owner: int = 0,
    lock_group: int = 0,
    lock_timeout_s: float = WRITE_TIMEOUT_S,
) -> Outcome:
    """The read-modify-write of the floor under the exclusive lock (raises KernelError, FloorLockError)."""
    with floor_lock(exclusive=True, timeout_s=lock_timeout_s, path=floor_dir / ".lock", owner=lock_owner,
                    group=lock_group):  # fmt: skip
        old_state = load_state(floor_dir / STATE_FILE)
        was_known = old_state is not None and old_state.known  # what the files said after the last run
        old_public_text = _read(floor_dir / PUBLIC_FILE)
        try:
            old_entries = parse_public_text(old_public_text) if old_public_text is not None else []
        except ValueError:
            old_entries = []  # an invalid file: rebuilt from the state (the kernel chain gets -C checked adds)
        state = next_state(old_state, v4, v6, now)
        entries = entries_of(state)
        new_public, new_code_web = public_text(entries), code_web_text(state.known)
        if new_public == old_public_text and new_code_web == _read(floor_dir / CODE_WEB_FILE):
            write_file(floor_dir / STATE_FILE, state.to_json())  # timestamps only; no reload
            return Outcome(False, state.known, len(state.v4), len(state.v6))
        kernel_update(old_entries, entries, ipt)  # raises before any file is touched
        state = state.model_copy(update={"generation": state.generation + 1})
        write_file(floor_dir / PUBLIC_FILE, new_public)
        write_file(floor_dir / CODE_WEB_FILE, new_code_web)
        write_file(floor_dir / STATE_FILE, state.to_json())
        hup()
    if was_known and not state.known:
        log.error("public floor unknown: no valid IPv4 answer for more than 300 s; repo code's public web is"
                  " paused", extra={"event": "public_floor_unknown"})  # fmt: skip
    return Outcome(True, state.known, len(state.v4), len(state.v6))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ossbot-pubip")
    parser.add_argument("--floor-dir", type=Path, default=FLOOR_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    v4, v6 = lookup_all()  # outside the lock: the lookups take up to 3 x 10 s
    try:
        out = update(args.floor_dir, v4, v6, time.time())
    except (KernelError, FloorLockError, OSError, ValueError) as exc:
        log.error("public floor not updated: %s", exc, extra={"event": "pubip_update_failed"})
        return 1
    log.info("public floor: known=%s, %d IPv4 and %d IPv6 entries%s", out.known, out.v4, out.v6,
             " (updated, proxy reloaded)" if out.changed else "")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
