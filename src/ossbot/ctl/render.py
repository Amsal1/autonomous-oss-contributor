"""Renderers for the egress layer (DESIGN §11.2-11.4): squid.conf and the ossbot-firewall chains.

squid: from the config snapshot's ``egress.yaml`` (+ ``profile.kiro`` for ``kiro_idc_login``,
``targets.yaml`` ``egress_extra`` for ``pkg_helm``), ``dns_nameservers`` from ``/etc/resolv.conf``.
Sandboxes are classed by source pool (§10.3); profiles sharing a pool must have identical
categories, otherwise rendering fails (squid could not tell them apart).

firewall: ``OSSBOT-IN``/``OSSBOT-OUT`` (+ ip6tables) for ``ossbot-firewall.service`` (part
``host``) and ``OSSBOT-FWD`` in ``DOCKER-USER`` for ``ossbot-firewall-docker.service`` (part
``docker``). ``--apply`` flushes and refills each chain and leaves exactly one jump at position
1 of its parent; ``--check`` compares chain contents (order included) and jump positions and
exits 1 on any drift, including a changed resolver (``firewall_drift:resolver_changed``) and,
for the host part, a systemd-resolved that is not masked or still listening
(``firewall_drift:resolved_ipc``, Phase 1 review P1-6) and io_uring not disabled system-wide
(``firewall_drift:io_uring``, P1-14).

Egress modes (§11.5, CR-002): ``squid`` renders base lists plus the grants of the active mode (the
``egress-mode`` control flag, else the snapshot's ``mode_default``). Every render first renders all
three modes and refuses if an invariant differs (denied networks and names, IP-literal and DNS
lines, fetch/validate and runner/gatekeeper lists), and each context is asserted on its own (fetch,
validate, runner, gatekeeper never ``web_public``; ``allowlist`` has none; agent-llm/catalog only in
``open``). ``--check`` (host part) also compares the live squid.conf with the render for the current
mode (``firewall_drift:squid_mode``).

Usage (root): ``python -I -m ossbot.ctl.render firewall --apply|--check|--print [--part P]`` and
``python -I -m ossbot.ctl.render squid [--out PATH] [--hermes-login] [--mode M]``.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import pwd
import re
import socket
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import jinja2

from ossbot.common.config import (
    TELEGRAPH_CATEGORY,
    ConfigBundle,
    ConfigError,
    EgressConfig,
    ProfileConfig,
    TargetsConfig,
    helm_hosts,
    parse_bundle,
)
from ossbot.common.control_flags import EGRESS_MODE_FILE, EGRESS_MODES, EgressMode, read_egress_mode
from ossbot.common.snapshot import read_current, read_snapshot

PROXY_IP = "10.212.0.2"
HOST_BRIDGE = "10.212.0.1/32"
EGRESS_NET = "10.212.0.0/24"
TASK_NET = "10.213.0.0/16"
SANDBOX_PORT = 3128
# Squid source pools (DESIGN §10.3) -> the egress.yaml sandbox profiles that share each pool.
SQUID_POOLS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("fetch", "10.213.0.0/18", ("fetch", "validate")),
    ("research", "10.213.64.0/18", ("research",)),
    ("llm", "10.213.128.0/18", ("agent-llm", "catalog")),
)
# Strict repos (design §12.3, ADR-0023): fetch/validate jobs of an `egress: strict` target get a /28 in
# this pool and the fetch base list in every egress mode. Not a SQUID_POOLS tuple (it has no profile of
# its own): squid_context renders it explicitly. 10.213.224.0/19 stays reserved for the Phase-2 stack.
STRICT_POOL = ("strict", "10.213.192.0/19")
STRICT_POOL_SRC = "pool_strict"
STRICT_BASE_PROFILE = "fetch"
HOST_CALLER_ORDER = ("runner", "hermes", "gatekeeper")
SERVICE_USERS = ("ossrunner", "hermes", "gatekeeper", "ossbox", "ossnobody")
DEFAULT_TEMPLATE = Path("/opt/ossbot/app/sandbox/proxy/squid.conf.j2")
DEFAULT_SQUID_OUT = Path("/etc/ossbot/proxy/squid.conf")
DEFAULT_SNAPSHOT_ROOT = Path("/srv/ossbot/config-snapshot")
RESOLVED_UNIT_LINK = Path("/etc/systemd/system/systemd-resolved.service")
RESOLVED_VARLINK = Path("/run/systemd/resolve/io.systemd.Resolve")
IO_URING_SYSCTL = Path("/proc/sys/kernel/io_uring_disabled")
# Request hosts that are IP literals in any form getaddrinfo/inet_aton accepts (dotted, short,
# decimal, octal, hex; IPv6 with or without brackets). POSIX ERE for squid `dstdom_regex -n -i`;
# no DNS name can match (a TLD is never all-numeric, and no host name contains ':' or '[').
IP_LITERAL_PATTERNS = (r"^(0x[0-9a-f]*|[0-9]+)(\.(0x[0-9a-f]*|[0-9]+)){0,3}\.?$", ":", r"^\[")
DEFAULT_CONTROL_DIR = Path("/var/lib/ossbot/control")
# Present only while `ossctl hermes login` has the IdC login window open (root-only /run, tmpfs, so a
# reboot clears it); squid_drift reads the real window state from it, not from the conf under test.
LOGIN_WINDOW_FLAG = Path("/run/ossbot-hermes-login-window")
# §11.5 invariants no egress mode relaxes, asserted by the renderer on top of the data (decision B3).
REQUIRED_DENIED_CIDRS = (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "127.0.0.0/8",
    "0.0.0.0/8", "::/128", "::1/128", "fc00::/7", "fe80::/10",
)  # fmt: skip
REQUIRED_DENIED_DOMAINS = ("uploads.github.com", "telegra.ph", "api.telegra.ph", "edit.telegra.ph")
# fetch + validate, and agent-exec under implement_network (ADR-0024): untrusted code, web_public only in
# sandbox_open
CODE_POOL_SRC = "pool_fetch"
CODE_WEB_MODE = "sandbox_open"
LLM_POOL_SRC = "pool_llm"
LLM_WEB_MODES = ("open", "sandbox_open")
TRUSTED_CALLERS = ("runner", "gatekeeper")  # identical egress in every mode
# A mode switch writes the flag first and the root apply re-renders a moment later: a conf older
# than a flag this young is "apply pending", not drift (the watchdog re-checks within 9 s anyway).
MODE_APPLY_GRACE_S = 60.0


class RenderError(ValueError):
    pass


# ------------------------------------------------------------------------------- resolver
def nameservers(resolv_conf: Path = Path("/etc/resolv.conf")) -> list[str]:
    """IPv4 nameservers in order (WSL with dnsTunneling: 10.255.255.254, never hard-coded)."""
    out: list[str] = []
    for line in resolv_conf.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            try:
                if ipaddress.ip_address(parts[1]).version == 4:
                    out.append(parts[1])
            except ValueError:
                continue
    if not out:
        raise RenderError(f"no IPv4 nameserver in {resolv_conf}")
    return out


# ---------------------------------------------------------------------------------- squid
def idc_login_hosts(profile: ProfileConfig) -> list[str]:
    host = urlparse(profile.kiro.idc_start_url).hostname
    region = profile.kiro.idc_region
    return [
        *([host] if host else []),
        f"oidc.{region}.amazonaws.com",
        f"portal.sso.{region}.amazonaws.com",
        f"{region}.signin.aws",
        f"{region}.signin.aws.amazon.com",
    ]


def _dedupe(hosts: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(h.lower() for h in hosts))


def resolve_categories(
    egress: EgressConfig, profile: ProfileConfig, targets: TargetsConfig, hermes_login: bool
) -> dict[str, list[str]]:
    """Category -> hosts actually rendered (empty, denied-by-default and inactive ones dropped)."""
    cats = {name: _dedupe(hosts) for name, hosts in egress.categories.items() if name not in egress.denied_by_default}
    if hermes_login and "kiro_idc_login" not in egress.denied_by_default:
        cats["kiro_idc_login"] = _dedupe(idc_login_hosts(profile))
    helm = _dedupe(helm_hosts(targets))
    if helm and "pkg_helm" not in egress.denied_by_default:
        cats["pkg_helm"] = helm
    return {k: v for k, v in cats.items() if v}


def _dst(category: str, cats: Mapping[str, list[str]]) -> str | None:
    if category == "web_public":
        return "all"
    return f"cat_{category}" if category in cats else None


def mode_lists(egress: EgressConfig, mode: EgressMode) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """(sandbox profile -> categories, host caller -> categories) for ``mode``: base + the mode's grants."""
    grants: dict[str, list[str]] = {str(k): v for k, v in egress.modes.get(mode, {}).items()}

    def plus(base: Sequence[str], name: str) -> list[str]:
        return [*base, *[c for c in grants.get(name, []) if c not in base]]

    profiles = {str(p): plus(cats, str(p)) for p, cats in egress.sandbox_profiles.items()}
    callers = {str(c): plus(hc.categories, str(c)) for c, hc in egress.host_callers.items()}
    return profiles, callers


def squid_context(
    bundle: ConfigBundle,
    snapshot_hash: str,
    ns: Sequence[str],
    hermes_login: bool = False,
    mode: EgressMode | None = None,
) -> dict[str, Any]:
    egress = bundle.egress
    mode = mode or egress.mode_default
    cats = resolve_categories(egress, bundle.profile, bundle.targets, hermes_login)
    callers = egress.host_callers
    profile_lists, caller_lists = mode_lists(egress, mode)
    listeners = [{"bind": str(SANDBOX_PORT), "name": "sandbox"}]
    listeners += [{"bind": f"{PROXY_IP}:{callers[c].port}", "name": c} for c in HOST_CALLER_ORDER if c in callers]
    rules: list[dict[str, str]] = []
    pools = []
    for pool, cidr, profiles in SQUID_POOLS:
        lists = [profile_lists.get(p) for p in profiles]
        present = [lst for lst in lists if lst is not None]
        if not present:
            continue
        if any(lst != present[0] for lst in present):
            raise RenderError(f"profiles {profiles} share pool {cidr} but have different categories")
        pools.append({"name": pool, "cidr": cidr})
        for category in present[0]:
            dst = _dst(category, cats)
            if dst is not None:
                rules.append({"port": "sandbox", "src": f"pool_{pool}", "dst": dst})
    # pool_strict: the fetch BASE list (mode_lists is never consulted), identical in every mode
    if STRICT_BASE_PROFILE in egress.sandbox_profiles:
        pools.append({"name": STRICT_POOL[0], "cidr": STRICT_POOL[1]})
        for category in egress.sandbox_profiles[STRICT_BASE_PROFILE]:
            dst = _dst(category, cats)
            if dst is not None:
                rules.append({"port": "sandbox", "src": STRICT_POOL_SRC, "dst": dst})
    for caller in HOST_CALLER_ORDER:
        if caller not in caller_lists:
            continue
        for category in caller_lists[caller]:
            dst = _dst(category, cats)
            if dst is not None:
                rules.append({"port": caller, "src": "src_host", "dst": dst})
    # §14.12.8: the gatekeeper's opt-in Telegraph egress, ahead of the always_denied line (which keeps
    # telegra.ph denied for every other caller); absent from the render while telegraph.enabled is false
    pre_deny: list[dict[str, str]] = []
    if bundle.telegram.telegraph.enabled and TELEGRAPH_CATEGORY in cats and "gatekeeper" in callers:
        pre_deny.append({"port": "gatekeeper", "src": "src_host", "dst": f"cat_{TELEGRAPH_CATEGORY}"})
    referenced = {r["dst"][4:] for r in [*rules, *pre_deny] if r["dst"].startswith("cat_")}
    ctx = {
        "snapshot_hash": snapshot_hash,
        "mode": mode,
        "listeners": listeners,
        "nameservers": list(ns),
        "host_src": HOST_BRIDGE,
        "pools": pools,
        "categories": {k: v for k, v in cats.items() if k in referenced},
        "denied_domains": _dedupe(egress.always_denied.domains),
        "ip_literal_patterns": list(IP_LITERAL_PATTERNS),
        # never render anything but literal networks into the dst ACL
        "denied_cidrs": [str(ipaddress.ip_network(c, strict=True)) for c in egress.always_denied.cidrs],
        "rules": rules,
        "pre_deny_rules": pre_deny,
    }
    assert_context_invariants(ctx)
    return ctx


def assert_context_invariants(ctx: Mapping[str, Any]) -> None:
    """§11.5 invariants of one rendered context (assertions, not data): raise RenderError on a violation."""
    rules: list[dict[str, str]] = ctx["rules"]
    for r in rules:
        if r["src"] == STRICT_POOL_SRC and r["dst"] == "all":
            raise RenderError("strict fetch/validate jobs (pool_strict) must never get web_public")
        if r["src"] == CODE_POOL_SRC and r["dst"] == "all" and ctx["mode"] != CODE_WEB_MODE:
            raise RenderError(f"fetch/validate/agent-exec get web_public only in egress mode {CODE_WEB_MODE}")
        if r["port"] in TRUSTED_CALLERS and r["dst"] == "all":
            raise RenderError(f"host caller {r['port']} must never get web_public")
    web = {(r["port"], r["src"]) for r in rules if r["dst"] == "all"}
    if ctx["mode"] == "allowlist" and web:
        raise RenderError(f"egress mode allowlist grants web_public to {sorted(web)}")
    if ctx["mode"] not in LLM_WEB_MODES and ("sandbox", LLM_POOL_SRC) in web:
        raise RenderError(f"egress mode {ctx['mode']} grants web_public to agent-llm/catalog (open/sandbox_open only)")
    denied = {str(ipaddress.ip_network(c)) for c in ctx["denied_cidrs"]}
    missing = [c for c in REQUIRED_DENIED_CIDRS if str(ipaddress.ip_network(c)) not in denied]
    if missing:
        raise RenderError(f"always_denied.cidrs lacks the invariant networks {missing}")
    missing_domains = [d for d in REQUIRED_DENIED_DOMAINS if d not in ctx["denied_domains"]]
    if missing_domains:
        raise RenderError(f"always_denied.domains lacks {missing_domains}")
    if tuple(ctx["ip_literal_patterns"]) != IP_LITERAL_PATTERNS:
        raise RenderError("IP-literal deny patterns changed")
    for r in ctx.get("pre_deny_rules", []):  # only the gatekeeper's Telegraph rule may precede the denies
        if r != {"port": "gatekeeper", "src": "src_host", "dst": f"cat_{TELEGRAPH_CATEGORY}"}:
            raise RenderError(f"unexpected rule ahead of the denies: {r}")
    if any(r["dst"] == f"cat_{TELEGRAPH_CATEGORY}" for r in rules):
        raise RenderError("the telegraph category is rendered only for the gatekeeper, ahead of the denies")


def _invariant_view(ctx: Mapping[str, Any]) -> dict[str, Any]:
    """The parts of a context that must be identical in every egress mode (§11.5; design §12.2): the
    strict pool, the fetch pool's base rules (sandbox_open only adds its ``all`` line), the trusted
    callers, their categories, listeners, pools, denies and the IP-literal patterns."""
    fixed = [
        r for r in ctx["rules"]
        if r["src"] == STRICT_POOL_SRC
        or (r["src"] == CODE_POOL_SRC and r["dst"] != "all")
        or r["port"] in TRUSTED_CALLERS
    ]  # fmt: skip
    used = {r["dst"][4:] for r in fixed if r["dst"].startswith("cat_")}
    return {
        "listeners": ctx["listeners"],
        "nameservers": ctx["nameservers"],
        "host_src": ctx["host_src"],
        "pools": ctx["pools"],
        "denied_domains": ctx["denied_domains"],
        "denied_cidrs": ctx["denied_cidrs"],
        "ip_literal_patterns": ctx["ip_literal_patterns"],
        "strict_fetch_validate_runner_gatekeeper_rules": fixed,
        "pre_deny_rules": ctx["pre_deny_rules"],
        "their_categories": {k: v for k, v in ctx["categories"].items() if k in used},
    }


def check_mode_invariants(bundle: ConfigBundle, ns: Sequence[str], hermes_login: bool = False) -> None:
    """Render every egress mode and refuse if any §11.5 invariant differs between them."""
    views = {m: _invariant_view(squid_context(bundle, "", ns, hermes_login, m)) for m in EGRESS_MODES}
    base = views[EGRESS_MODES[0]]
    for mode, view in views.items():
        for key, value in base.items():
            if view[key] != value:
                raise RenderError(f"egress mode {mode} changes the invariant {key}")


def render_context(ctx: Mapping[str, Any], template: str) -> str:
    for server in ctx["nameservers"]:
        ipaddress.IPv4Address(server)  # never render anything but literal IPv4 addresses
    assert_context_invariants(ctx)
    # Not HTML: every value is validated (HOST_RE hosts, literal IPs, fixed names), so no escaping.
    env = jinja2.Environment(
        autoescape=False,  # noqa: S701
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )
    return env.from_string(template).render(**ctx)


def render_squid(
    bundle: ConfigBundle,
    snapshot_hash: str,
    ns: Sequence[str],
    template: str,
    hermes_login: bool = False,
    mode: EgressMode | None = None,
) -> str:
    for server in ns:
        ipaddress.IPv4Address(server)
    check_mode_invariants(bundle, ns, hermes_login)
    return render_context(squid_context(bundle, snapshot_hash, ns, hermes_login, mode), template)


_SNAPSHOT_LINE_RE = re.compile(r"^# Config snapshot: ([0-9a-f]{64})$", re.MULTILINE)


def _snapshot_rejection_pending(snapshot_root: Path, digest: str, now: float | None) -> bool:
    """A conf whose snapshot the schema rejects is tolerated while that snapshot is still current,
    while the newer current one is younger than MODE_APPLY_GRACE_S (proxy restart pending), or while
    the current one is rejected too: the gatekeeper validates every snapshot it writes, so a rejected
    current snapshot means it has not started on the deployed code yet. Once it has written a valid
    one, only the grace applies."""
    link = snapshot_root / "current"
    try:
        current = os.readlink(link)
        if current == digest:
            return True
        if (now or time.time()) - link.lstat().st_mtime < MODE_APPLY_GRACE_S:
            return True
        files = read_snapshot(snapshot_root, current)
    except (OSError, ValueError):
        return False
    try:
        parse_bundle(files)
    except ConfigError:
        return True  # gatekeeper restart pending (deploy of a schema change)
    return False


def squid_snapshot_rejected(conf: Path = DEFAULT_SQUID_OUT, snapshot_root: Path = DEFAULT_SNAPSHOT_ROOT) -> str | None:
    """The snapshot hash the live conf names when the installed schema rejects it (the drift check
    cannot verify the conf then; doctor reports it), else None."""
    try:
        match = _SNAPSHOT_LINE_RE.search(conf.read_text(encoding="utf-8"))
        files = read_snapshot(snapshot_root, match.group(1)) if match else None
    except (OSError, UnicodeDecodeError, ValueError):
        return None  # unreadable or missing: squid_drift reports those itself
    if match is None or files is None:
        return None
    try:
        parse_bundle(files)
    except ConfigError:
        return match.group(1)
    return None


def squid_drift(
    conf: Path = DEFAULT_SQUID_OUT,
    snapshot_root: Path = DEFAULT_SNAPSHOT_ROOT,
    template: Path = DEFAULT_TEMPLATE,
    resolv_conf: Path = Path("/etc/resolv.conf"),
    control_dir: Path = DEFAULT_CONTROL_DIR,
    now: float | None = None,
    login_window_flag: Path = LOGIN_WINDOW_FLAG,
) -> list[str]:
    """The live squid.conf must equal the render of its snapshot for the current egress mode.

    No conf yet is not drift (the proxy refuses to start without one). The snapshot is the one the
    conf names (snapshots are kept), so a confirmed reload is not drift before the proxy restart;
    the mode is always the current flag (fail closed ``allowlist``). The Hermes IdC login window is
    the real window state: a conf with the window open is drift unless ``ossctl hermes login`` holds
    ``login_window_flag`` (while it does, either render is accepted, so its open and close steps never
    count as drift)."""
    tag = "firewall_drift:squid_mode"
    try:
        text = conf.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{tag} cannot read {conf}: {exc}"]
    match = _SNAPSHOT_LINE_RE.search(text)
    if match is None:
        return [f"{tag} {conf} names no config snapshot"]
    digest = match.group(1)
    try:
        files = read_snapshot(snapshot_root, digest)
    except (OSError, ValueError) as exc:
        return [f"{tag} cannot read the snapshot {digest[:12]} the conf names: {exc}"]
    try:
        bundle = parse_bundle(files)
    except ConfigError:
        # A deploy changed the schema and the installed code no longer accepts the (hash-verified,
        # gatekeeper-written) snapshot the live conf came from. The gatekeeper writes a new snapshot
        # at its next start and the proxy re-renders at its restart; refusing here would block the
        # very gatekeeper start that fixes it (every unit runs --check first). Not verifiable yet,
        # but only while that snapshot is still the current one (doctor WARNs meanwhile), a newer
        # one is younger than the proxy-restart grace, or the current one is rejected too (the
        # gatekeeper has not started on the new code); after that the proxy missed its re-render.
        if _snapshot_rejection_pending(snapshot_root, digest, now):
            return []
        return [f"{tag} {conf} names snapshot {digest[:12]}, which the installed schema rejects, and a "
                "newer snapshot is current (restart ossbot-proxy)"]  # fmt: skip
    try:
        mode = read_egress_mode(control_dir, bundle.egress.mode_default)
        expected = render_squid(
            bundle, digest, nameservers(resolv_conf), template.read_text(encoding="utf-8"),
            hermes_login=login_window_flag.exists() and "\nacl cat_kiro_idc_login " in text, mode=mode,
        )  # fmt: skip
    except (OSError, ValueError) as exc:
        return [f"{tag} cannot render the expected config: {exc}"]
    if expected == text:
        return []
    try:
        flag_mtime = (control_dir / EGRESS_MODE_FILE).stat().st_mtime
        pending = flag_mtime > conf.stat().st_mtime and (now or time.time()) - flag_mtime < MODE_APPLY_GRACE_S
    except OSError:
        pending = False
    if pending:
        return []
    return [f"{tag} {conf} differs from the render for egress mode {mode}"]


# ------------------------------------------------------------------------------- firewall
@dataclass(frozen=True)
class Chain:
    family: str  # iptables | ip6tables
    name: str
    parent: str
    rules: tuple[tuple[str, ...], ...]


def firewall_plan(ns: str, uids: Mapping[str, str] | None = None) -> dict[str, list[Chain]]:
    """Exactly the rules of DESIGN §11.4 (``uids`` maps account names to the value to render)."""
    ipaddress.IPv4Address(ns)
    u = {name: (uids or {}).get(name, name) for name in SERVICE_USERS}
    host_in = (
        ("-s", f"{PROXY_IP}/32", "-d", f"{ns}/32", "-p", "udp", "--dport", "53", "-j", "ACCEPT"),
        ("-s", f"{PROXY_IP}/32", "-d", f"{ns}/32", "-p", "tcp", "--dport", "53", "-j", "ACCEPT"),
        ("-s", TASK_NET, "-j", "DROP"),
        ("-s", EGRESS_NET, "-m", "conntrack", "--ctstate", "NEW", "-j", "DROP"),
    )

    # DNS (port 53, any destination) is refused for every service uid before the lo ACCEPT: the WSL
    # resolver (10.255.255.254) and a 127.0.0.53 stub both sit on lo, so the lo ACCEPT alone would
    # be a direct DNS channel past squid (Phase 1 review P1-1, DESIGN §11.4 "no direct UDP 53").
    def no_dns(owner: tuple[str, ...], verdict: str) -> list[tuple[str, ...]]:
        return [(*owner, "-p", proto, "--dport", "53", "-j", verdict) for proto in ("udp", "tcp")]

    out: list[tuple[str, ...]] = []
    for user, port in (("ossrunner", 3140), ("hermes", 3141), ("gatekeeper", 3150)):
        owner = ("-m", "owner", "--uid-owner", u[user])
        out.extend(no_dns(owner, "REJECT"))
        out.append((*owner, "-d", PROXY_IP, "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"))
        out.append((*owner, "-o", "lo", "-j", "ACCEPT"))
        out.append((*owner, "-j", "REJECT"))
    for user in ("ossbox", "ossnobody"):
        owner = ("-m", "owner", "--uid-owner", u[user])
        out.extend(no_dns(owner, "REJECT"))
        out.append((*owner, "-o", "lo", "-j", "ACCEPT"))
        out.append((*owner, "-j", "REJECT"))
    out6: list[tuple[str, ...]] = []
    for user in SERVICE_USERS:
        owner = ("-m", "owner", "--uid-owner", u[user])
        out6.extend(no_dns(owner, "DROP"))
        out6.append((*owner, "-o", "lo", "-j", "ACCEPT"))
        out6.append((*owner, "-j", "DROP"))
    fwd6 = (
        ("-i", "docker0", "-j", "DROP"), ("-o", "docker0", "-j", "DROP"),
        ("-i", "br-+", "-j", "DROP"), ("-o", "br-+", "-j", "DROP"),
    )  # fmt: skip
    # No IPv6 from any container to a host service (link-local on the bridges included); the IPv4
    # counterpart is OSSBOT-IN by source subnet. Egress review: the IPv6 row of the threat table.
    in6 = (("-i", "docker0", "-j", "DROP"), ("-i", "br-+", "-j", "DROP"))
    return {
        "host": [
            Chain("iptables", "OSSBOT-IN", "INPUT", host_in),
            Chain("iptables", "OSSBOT-OUT", "OUTPUT", tuple(out)),
            Chain("ip6tables", "OSSBOT-OUT", "OUTPUT", tuple(out6)),
            Chain("ip6tables", "OSSBOT-IN", "INPUT", in6),
            Chain("ip6tables", "OSSBOT-FWD", "FORWARD", fwd6),
        ],
        "docker": [
            Chain("iptables", "OSSBOT-FWD", "DOCKER-USER", (("-s", TASK_NET, "!", "-d", TASK_NET, "-j", "DROP"),)),
        ],
    }


def select(plan: Mapping[str, list[Chain]], part: str) -> list[Chain]:
    if part == "all":
        return [*plan["host"], *plan["docker"]]
    return list(plan[part])


def render_script(chains: Iterable[Chain]) -> str:
    """The shell form of §11.4 (golden-file and ``--print`` output)."""
    lines: list[str] = []
    for c in chains:
        f = c.family
        lines.append(f"{f} -N {c.name} 2>/dev/null || {f} -F {c.name}")
        lines.extend(f"{f} -A {c.name} {' '.join(rule)}" for rule in c.rules)
        lines.append(f"{f} -C {c.parent} -j {c.name} 2>/dev/null || {f} -I {c.parent} 1 -j {c.name}")
    return "\n".join(lines) + "\n"


_VALUE_OPTS = {
    "-s", "-d", "-i", "-o", "-p", "--dport", "--sport", "--uid-owner", "--ctstate", "-j", "-m", "--reject-with",
}  # fmt: skip
_IMPLICIT_MODULES = {"udp", "tcp"}
# `iptables -S` prints the REJECT default explicitly (verified live on iptables 1.8.10 nf_tables).
_DEFAULT_REJECT = {"icmp-port-unreachable", "icmp6-port-unreachable"}


def normalize_rule(tokens: Sequence[str]) -> frozenset[tuple[str, str]]:
    """Order-insensitive form of one rule as printed by ``iptables -S`` or as rendered."""
    items: set[tuple[str, str]] = set()
    i, negate = 0, False
    while i < len(tokens):
        tok = tokens[i]
        if tok == "!":
            negate = True
            i += 1
            continue
        if tok not in _VALUE_OPTS or i + 1 >= len(tokens):
            items.add(("?", tok))  # anything unexpected makes the rule differ (drift), never matches
            i += 1
            continue
        value = tokens[i + 1]
        i += 2
        if (tok == "-m" and value in _IMPLICIT_MODULES) or (tok == "--reject-with" and value in _DEFAULT_REJECT):
            continue
        if tok in {"-s", "-d"} and "/" not in value:
            value = f"{value}/32" if ":" not in value else f"{value}/128"
        items.add(((f"!{tok}" if negate else tok), value))
        negate = False
    return frozenset(items)


def parse_listing(listing: str, chain: str) -> list[frozenset[tuple[str, str]]]:
    """Rules of ``chain`` from ``iptables -S <chain>`` output, in order."""
    out = []
    prefix = f"-A {chain} "
    for line in listing.splitlines():
        if line.startswith(prefix):
            out.append(normalize_rule(line[len(prefix) :].split()))
    return out


def check_chain(chain: Chain, chain_listing: str, parent_listing: str) -> list[str]:
    problems: list[str] = []
    want = [normalize_rule(r) for r in chain.rules]
    have = parse_listing(chain_listing, chain.name)
    if have != want:
        problems.append(f"{chain.family} {chain.name}: rules differ from the rendered set")
    jump = frozenset({("-j", chain.name)})
    parent_rules = parse_listing(parent_listing, chain.parent)
    if not parent_rules or parent_rules[0] != jump:
        problems.append(f"{chain.family} {chain.parent}: jump to {chain.name} is not at position 1")
    if parent_rules.count(jump) != 1:
        problems.append(f"{chain.family} {chain.parent}: expected exactly one jump to {chain.name}")
    return problems


def resolver_drift(chain_listing: str, ns: str) -> bool:
    """True when OSSBOT-IN accepts DNS for a different resolver than /etc/resolv.conf now names."""
    for rule in parse_listing(chain_listing, "OSSBOT-IN"):
        dst = dict(rule).get("-d")
        if dict(rule).get("--dport") == "53" and dst is not None and dst != f"{ns}/32":
            return True
    return False


def _ipt(family: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run([family, "-w", *args], capture_output=True, text=True, check=check)


def apply(chains: Iterable[Chain]) -> None:
    for c in chains:
        if _ipt(c.family, "-N", c.name, check=False).returncode != 0:
            _ipt(c.family, "-F", c.name)
        for rule in c.rules:
            _ipt(c.family, "-A", c.name, *rule)
        while _ipt(c.family, "-D", c.parent, "-j", c.name, check=False).returncode == 0:
            pass
        _ipt(c.family, "-I", c.parent, "1", "-j", c.name)


def check(chains: Iterable[Chain], ns: str) -> list[str]:
    problems: list[str] = []
    for c in chains:
        listing = _ipt(c.family, "-S", c.name, check=False)
        parent = _ipt(c.family, "-S", c.parent, check=False)
        if listing.returncode != 0:
            problems.append(f"{c.family} {c.name}: chain missing")
            continue
        if c.name == "OSSBOT-IN" and resolver_drift(listing.stdout, ns):
            problems.append("firewall_drift:resolver_changed")
        problems.extend(check_chain(c, listing.stdout, parent.stdout if parent.returncode == 0 else ""))
    return problems


def resolved_ipc_problems(unit_link: Path = RESOLVED_UNIT_LINK, varlink: Path = RESOLVED_VARLINK) -> list[str]:
    """systemd-resolved must be masked and not listening (Phase 1 review P1-6).

    It answers any uid over its 0666 varlink socket and over D-Bus, then queries upstream as
    ``systemd-resolve``, which the owner-match rules of OSSBOT-OUT do not cover: a DNS channel past
    squid. Nothing in the distro needs it (resolv.conf names the WSL resolver directly)."""
    problems: list[str] = []
    try:
        masked = os.readlink(unit_link) == "/dev/null"
    except OSError:
        masked = False
    if not masked:
        problems.append("firewall_drift:resolved_ipc systemd-resolved is not masked")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2.0)
            sock.connect(str(varlink))
        problems.append("firewall_drift:resolved_ipc systemd-resolved varlink socket accepts connections")
    except OSError:
        pass
    return problems


def io_uring_problems(sysctl: Path = IO_URING_SYSCTL) -> list[str]:
    """io_uring must be off for every process: ``kernel.io_uring_disabled = 2`` (Phase 1 review P1-14).

    ``IORING_OP_SOCKET`` creates sockets without the ``socket()`` syscall, so the seccomp filter of
    ``RestrictAddressFamilies=`` never sees the family: an ``AF_VSOCK`` socket to the Windows host
    (WSL init, the drvfs Plan 9 server) from a confined unit. A kernel without the sysctl fails."""
    try:
        value = sysctl.read_text(encoding="ascii").strip()
    except OSError:
        value = "unavailable"
    if value == "2":
        return []
    return [f"firewall_drift:io_uring kernel.io_uring_disabled is {value}, expected 2"]


def resolve_uids() -> dict[str, str]:
    out = {}
    for name in SERVICE_USERS:
        try:
            out[name] = str(pwd.getpwnam(name).pw_uid)
        except KeyError as exc:
            raise RenderError(f"account {name} does not exist") from exc
    return out


# ------------------------------------------------------------------------------------ cli
def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o644)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ossbot-render")
    sub = parser.add_subparsers(dest="what", required=True)
    fw = sub.add_parser("firewall")
    mode = fw.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--print", action="store_true")
    fw.add_argument("--part", choices=("host", "docker", "all"), default="all")
    fw.add_argument("--resolv-conf", type=Path, default=Path("/etc/resolv.conf"))
    sq = sub.add_parser("squid")
    sq.add_argument("--out", type=Path, default=DEFAULT_SQUID_OUT)
    sq.add_argument("--snapshot-root", type=Path, default=DEFAULT_SNAPSHOT_ROOT)
    sq.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    sq.add_argument("--resolv-conf", type=Path, default=Path("/etc/resolv.conf"))
    sq.add_argument("--hermes-login", action="store_true")
    # Default: the current egress-mode flag (absent = the snapshot's mode_default, unparseable = allowlist).
    sq.add_argument("--mode", choices=EGRESS_MODES, default=None)
    sq.add_argument("--control-dir", type=Path, default=DEFAULT_CONTROL_DIR)
    args = parser.parse_args(argv)
    try:
        if args.what == "squid":
            digest, files = read_current(args.snapshot_root)
            bundle = parse_bundle(files)
            egress: EgressMode = args.mode or read_egress_mode(args.control_dir, bundle.egress.mode_default)
            text = render_squid(
                bundle, digest, nameservers(args.resolv_conf), args.template.read_text("utf-8"),
                args.hermes_login, egress,
            )  # fmt: skip
            try:
                same = args.out.read_text(encoding="utf-8") == text
            except (OSError, UnicodeDecodeError):
                same = False
            if same:  # idempotent (provision prints `unchanged`); the running proxy is untouched
                print(f"unchanged {args.out} (snapshot {digest}, egress mode {egress})")
                return 0
            _write_atomic(args.out, text)
            print(f"rendered {args.out} from snapshot {digest} (egress mode {egress})")
            return 0
        ns = nameservers(args.resolv_conf)[0]
        if args.print:
            print(render_script(select(firewall_plan(ns), args.part)), end="")
            return 0
        chains = select(firewall_plan(ns, resolve_uids()), args.part)
        if args.apply:
            apply(chains)
            print(f"ossbot-firewall: applied {len(chains)} chains (resolver {ns})")
            return 0
        problems = check(chains, ns)
        if args.part in ("host", "all"):
            problems.extend(resolved_ipc_problems())
            problems.extend(io_uring_problems())
            problems.extend(squid_drift(resolv_conf=args.resolv_conf))
        for problem in problems:
            print(f"ossbot-firewall --check: {problem}", file=sys.stderr)
        return 1 if problems else 0
    except (RenderError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"ossbot-render: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
