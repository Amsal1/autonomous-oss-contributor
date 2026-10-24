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
(``firewall_drift:io_uring``, P1-14). Part ``host`` also holds the three mangle chains that clamp the
TCP MSS (``OSSBOT-MSS-FWD/-OUT/-IN``, F1c: a path-MTU black hole to Fastly stalled PyPI and uploads).

Egress modes (§11.5, CR-002): ``squid`` renders base lists plus the grants of the active mode (the
``egress-mode`` control flag, else the snapshot's ``mode_default``). Every render first renders all
five modes and refuses if an invariant differs (denied networks and names, IP-literal and DNS
lines, fetch/validate and runner/gatekeeper lists, the ADR-0025 floors), and each context is asserted
on its own (fetch, validate, runner, gatekeeper never ``web_public``; ``allowlist`` has none;
agent-llm/catalog only in the LLM web modes; fetch/validate only in ``sandbox_open``/``sandbox_full``,
which render identically but the label). ``--check`` (host part) also compares the live squid.conf with
the render for the current mode (``firewall_drift:squid_mode``).

Floors (build-env design §4, ADR-0025): squid's marked sandbox denies ahead of the port checks, the split
``denied_dst``/``denied_dst_loop`` ACL with the live host addresses (``host_floor_cidrs``) and the
public-address floor file, ``code_denied`` for repo code; in the kernel the source-split LOG + DROP
rules of ``OSSBOT-IN``, the proxy floor of ``OSSBOT-FWD`` per floor range and host address, and
``OSSBOT-PUBFLOOR`` from ``/etc/ossbot/proxy/floor/public.txt``. ``--apply`` holds the root-only floor
lock exclusively for every part touching ``OSSBOT-FWD``/``OSSBOT-PUBFLOOR``, ``--check`` (with the squid
drift check) shared with a 10 s timeout, ``render squid``'s creation of the floor files exclusively
(carried criterion C-M1, ``ossbot.common.floor_lock``).

Usage (root): ``python -I -m ossbot.ctl.render firewall --apply|--check|--print [--part P]`` and
``python -I -m ossbot.ctl.render squid [--out PATH] [--hermes-login] [--mode M]``.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import pwd
import re
import shlex
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import nullcontext
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
from ossbot.common.control_flags import (
    CODE_WEB_MODES,
    EGRESS_MODE_FILE,
    EGRESS_MODES,
    LLM_WEB_MODES,
    EgressMode,
    read_egress_mode,
)
from ossbot.common.egress_log import IPV4_FLOOR_CIDRS, LOOP_CIDRS, REQUIRED_DENIED_CIDRS
from ossbot.common.floor_lock import CHECK_TIMEOUT_S, LOCK_PATH, WRITE_TIMEOUT_S, floor_lock
from ossbot.common.public_floor import (
    CODE_WEB_FILE,
    FLOOR_DIR,
    PUBLIC_FILE,
    code_web_text,
    parse_public_text,
    public_text,
    read_public,
)
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
# §11.5 invariants no egress mode relaxes, asserted by the renderer on top of the data (decision B3). The
# address floors (REQUIRED_DENIED_CIDRS, from ossbot.common.egress_log: build-env design §4.1) are UNIONED
# with the snapshot's always_denied.cidrs, so config can only add and a snapshot older than ADR-0025 renders
# the full floors right after the deploy.
REQUIRED_DENIED_DOMAINS = ("uploads.github.com", "telegra.ph", "api.telegra.ph", "edit.telegra.ph")
# ADR-0025: names no repo-code pool reaches in any mode (unioned with config code_denied, every kiro_* host and
# the IdC login hosts); the always-denied telemetry name that is a note, when the snapshot doesn't say.
REQUIRED_CODE_DENIED = (
    "api.telegram.org", ".telegram.org", "t.me", ".t.me", "telegram.me", ".telegram.me", ".kiro.dev",
)  # fmt: skip
CODE_DENIED_KIRO_CATEGORIES = ("kiro_core", "kiro_telemetry", "kiro_download")
DEFAULT_NOTIFY_ONLY = ("checkpoint-api.hashicorp.com",)
# fetch + validate, and agent-exec under implement_network (ADR-0024): untrusted code, web_public only in
# sandbox_open and sandbox_full (ADR-0025)
CODE_POOL_SRC = "pool_fetch"
LLM_POOL_SRC = "pool_llm"
TRUSTED_CALLERS = ("runner", "gatekeeper")  # identical egress in every mode
# The public-address floor files (build-env design §4.7) as squid sees them in its directory bind.
SQUID_PUBLIC_FILE_LINE = 'acl denied_dst dst "/etc/squid/floor/public.txt"'
SQUID_CODE_WEB_LINE = 'acl code_web_ok src "/etc/squid/floor/code-web.txt"'
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
    """(sandbox profile -> categories, host caller -> categories) for ``mode``: base + the mode's grants
    (a snapshot without ``modes.sandbox_full`` gives it sandbox_open's grants, ADR-0025)."""
    grants: dict[str, list[str]] = egress.mode_grants(mode)

    def plus(base: Sequence[str], name: str) -> list[str]:
        return [*base, *[c for c in grants.get(name, []) if c not in base]]

    profiles = {str(p): plus(cats, str(p)) for p, cats in egress.sandbox_profiles.items()}
    callers = {str(c): plus(hc.categories, str(c)) for c, hc in egress.host_callers.items()}
    return profiles, callers


def collapse_domains(names: Iterable[str]) -> list[str]:
    """Lower-cased, deduplicated, sorted, and without any name a dotted entry already covers (squid's
    dstdomain refuses ``t.me`` next to ``.t.me``: the dotted form matches the domain and every subdomain)."""
    uniq = sorted(set(_dedupe(names)))
    dotted = {n for n in uniq if n.startswith(".")}

    def covered(name: str) -> bool:
        bare = name.lstrip(".")
        labels = bare.split(".")
        parents = {"." + ".".join(labels[i:]) for i in range(1, len(labels))}
        return (not name.startswith(".") and "." + bare in dotted) or bool(parents & dotted)

    return [n for n in uniq if not covered(n)]


def code_denied_names(bundle: ConfigBundle) -> list[str]:
    """ADR-0025: config ``code_denied`` plus the built-ins, every kiro_* host and the IdC login hosts."""
    egress = bundle.egress
    kiro = [h for c in CODE_DENIED_KIRO_CATEGORIES for h in egress.categories.get(c, [])]
    return collapse_domains([*egress.code_denied, *REQUIRED_CODE_DENIED, *kiro, *idc_login_hosts(bundle.profile)])


def denied_cidr_union(egress: EgressConfig) -> list[str]:
    """The snapshot's always_denied.cidrs in their order, then every missing built-in floor (config only adds)."""
    out = [str(ipaddress.ip_network(c, strict=True)) for c in egress.always_denied.cidrs]
    out += [c for c in (str(ipaddress.ip_network(r)) for r in REQUIRED_DENIED_CIDRS) if c not in out]
    return list(dict.fromkeys(out))


def _run_ip(argv: Sequence[str]) -> str:
    proc = subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=10)
    if proc.returncode != 0:
        raise RenderError(f"{' '.join(argv)} failed ({proc.returncode}): {proc.stderr.strip()[:200]}")
    return proc.stdout


_INET_RE = re.compile(r"\binet (\d{1,3}(?:\.\d{1,3}){3})/\d{1,2}\b")
_DEFAULT_VIA_RE = re.compile(r"^default via (\d{1,3}(?:\.\d{1,3}){3})\b", re.MULTILINE)


def host_floor_cidrs(
    floor_cidrs: Iterable[str] = IPV4_FLOOR_CIDRS,
    resolv_conf: Path = Path("/etc/resolv.conf"),
    run: Callable[[Sequence[str]], str] = _run_ip,
) -> tuple[str, ...]:
    """The live host addresses as /32s (build-env design §4.1, BE-2.3): every distro address (``ip -4 -o addr
    show``), the default gateway (the Windows host in WSL NAT mode) and the resolv.conf nameservers, minus those
    already inside a static floor range; sorted. A failing ``ip`` fails the render (fail closed)."""
    found = set(_INET_RE.findall(run(["ip", "-4", "-o", "addr", "show"])))
    found.update(_DEFAULT_VIA_RE.findall(run(["ip", "-4", "route", "show", "default"])))
    found.update(nameservers(resolv_conf))
    statics = [ipaddress.ip_network(c) for c in floor_cidrs]
    keep = set()
    for raw in found:
        ip = ipaddress.IPv4Address(raw)
        if not any(ip.version == n.version and ip in n for n in statics):
            keep.add(ip)
    return tuple(f"{ip}/32" for ip in sorted(keep))


def squid_context(
    bundle: ConfigBundle,
    snapshot_hash: str,
    ns: Sequence[str],
    hermes_login: bool = False,
    mode: EgressMode | None = None,
    host_cidrs: Sequence[str] = (),
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
    # never render anything but literal networks into the dst ACLs
    denied_cidrs = denied_cidr_union(egress)
    loop = [str(ipaddress.ip_network(c)) for c in LOOP_CIDRS]
    hosts = [str(ipaddress.IPv4Network(h, strict=True)) for h in host_cidrs]
    if any(ipaddress.ip_network(h).prefixlen != 32 for h in hosts):
        raise RenderError(f"host floor entries must be /32s: {hosts}")
    notify_only = _dedupe(egress.always_denied.notify_only or DEFAULT_NOTIFY_ONLY)
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
        "denied_cidrs": denied_cidrs,
        # ADR-0025 floors (build-env design §4.3)
        "loop_cidrs": loop,
        "denied_cidrs_non_loop": [c for c in denied_cidrs if c not in loop],
        "host_cidrs": hosts,
        "notify_only": notify_only,
        "code_pool_src": CODE_POOL_SRC,
        "code_pool_cidrs": [SQUID_POOLS[0][1], STRICT_POOL[1]],
        "code_denied": code_denied_names(bundle),
        "rules": rules,
        "sandbox_rules": [r for r in rules if r["port"] == "sandbox"],
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
        if r["src"] == CODE_POOL_SRC and r["dst"] == "all" and ctx["mode"] not in CODE_WEB_MODES:
            raise RenderError(
                f"fetch/validate/agent-exec get web_public only in egress modes {'/'.join(CODE_WEB_MODES)}"
            )
        if r["port"] in TRUSTED_CALLERS and r["dst"] == "all":
            raise RenderError(f"host caller {r['port']} must never get web_public")
    web = {(r["port"], r["src"]) for r in rules if r["dst"] == "all"}
    if ctx["mode"] == "allowlist" and web:
        raise RenderError(f"egress mode allowlist grants web_public to {sorted(web)}")
    if ctx["mode"] not in LLM_WEB_MODES and ("sandbox", LLM_POOL_SRC) in web:
        raise RenderError(f"egress mode {ctx['mode']} grants web_public to agent-llm/catalog"
                          f" ({'/'.join(LLM_WEB_MODES)} only)")  # fmt: skip
    denied = {str(ipaddress.ip_network(c)) for c in ctx["denied_cidrs"]}
    missing = [c for c in REQUIRED_DENIED_CIDRS if str(ipaddress.ip_network(c)) not in denied]
    if missing:
        raise RenderError(f"always_denied.cidrs lacks the invariant networks {missing}")
    missing_domains = [d for d in REQUIRED_DENIED_DOMAINS if d not in ctx["denied_domains"]]
    if missing_domains:
        raise RenderError(f"always_denied.domains lacks {missing_domains}")
    _assert_floor_invariants(ctx)
    if tuple(ctx["ip_literal_patterns"]) != IP_LITERAL_PATTERNS:
        raise RenderError("IP-literal deny patterns changed")
    for r in ctx.get("pre_deny_rules", []):  # only the gatekeeper's Telegraph rule may precede the denies
        if r != {"port": "gatekeeper", "src": "src_host", "dst": f"cat_{TELEGRAPH_CATEGORY}"}:
            raise RenderError(f"unexpected rule ahead of the denies: {r}")
    if any(r["dst"] == f"cat_{TELEGRAPH_CATEGORY}" for r in rules):
        raise RenderError("the telegraph category is rendered only for the gatekeeper, ahead of the denies")


def _assert_floor_invariants(ctx: Mapping[str, Any]) -> None:
    """ADR-0025 floor invariants of one context (build-env design §4.3)."""
    loop = {str(ipaddress.ip_network(c)) for c in ctx["loop_cidrs"]}
    non_loop = {str(ipaddress.ip_network(c)) for c in ctx["denied_cidrs_non_loop"]}
    hosts = {str(ipaddress.ip_network(c)) for c in ctx["host_cidrs"]}
    denied = {str(ipaddress.ip_network(c)) for c in ctx["denied_cidrs"]}
    if loop != {str(ipaddress.ip_network(c)) for c in LOOP_CIDRS}:
        raise RenderError("denied_dst_loop must be exactly the loopback floors")
    if loop & (non_loop | hosts) or (loop | non_loop | hosts) != (denied | hosts):
        raise RenderError("denied_dst_loop and denied_dst must split denied_cidrs ∪ host_cidrs (disjoint)")
    notify = set(ctx["notify_only"])
    if not notify or not notify <= set(ctx["denied_domains"]) or notify & set(REQUIRED_DENIED_DOMAINS):
        raise RenderError("notify_only must be a non-empty subset of the always-denied names, never a required one")
    if ctx["code_pool_cidrs"] != [SQUID_POOLS[0][1], STRICT_POOL[1]]:
        raise RenderError("code_pools must be exactly pool_fetch and pool_strict")
    code = set(ctx["code_denied"])
    dotted = [d for d in code if d.startswith(".")]

    def covered(name: str) -> bool:
        return name in code or "." + name.lstrip(".") in code or any(name.endswith(d) for d in dotted)

    if not all(covered(n) for n in REQUIRED_CODE_DENIED):
        raise RenderError("code_denied lacks the built-in Telegram/Kiro names")
    if ctx["sandbox_rules"] != [r for r in ctx["rules"] if r["port"] == "sandbox"]:
        raise RenderError("every sandbox rule needs its floor lines")


def _invariant_view(ctx: Mapping[str, Any]) -> dict[str, Any]:
    """The parts of a context that must be identical in every egress mode (§11.5; design §12.2): the
    strict pool, the fetch pool's base rules (sandbox_open/sandbox_full only add its ``all`` line), the
    trusted callers, their categories, listeners, pools, denies, the IP-literal patterns and the ADR-0025
    floors (split dst ACLs, host addresses, notify_only, the repo-code pools and names)."""
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
        "loop_cidrs": ctx["loop_cidrs"],
        "denied_cidrs_non_loop": ctx["denied_cidrs_non_loop"],
        "host_cidrs": ctx["host_cidrs"],
        "notify_only": ctx["notify_only"],
        "code_pool_cidrs": ctx["code_pool_cidrs"],
        "code_denied": ctx["code_denied"],
        "ip_literal_patterns": ctx["ip_literal_patterns"],
        "strict_fetch_validate_runner_gatekeeper_rules": fixed,
        "pre_deny_rules": ctx["pre_deny_rules"],
        "their_categories": {k: v for k, v in ctx["categories"].items() if k in used},
    }


def check_mode_invariants(
    bundle: ConfigBundle, ns: Sequence[str], hermes_login: bool = False, host_cidrs: Sequence[str] = ()
) -> None:
    """Render every egress mode and refuse if any §11.5 invariant differs between them; sandbox_full must be
    sandbox_open but its label, and the llm pool must be identical in every LLM web mode (ADR-0025)."""
    ctxs = {m: squid_context(bundle, "", ns, hermes_login, m, host_cidrs) for m in EGRESS_MODES}
    views = {m: _invariant_view(c) for m, c in ctxs.items()}
    base = views[EGRESS_MODES[0]]
    for mode, view in views.items():
        for key, value in base.items():
            if view[key] != value:
                raise RenderError(f"egress mode {mode} changes the invariant {key}")
    if {**ctxs["sandbox_full"], "mode": "sandbox_open"} != ctxs["sandbox_open"]:
        raise RenderError("egress mode sandbox_full must render exactly what sandbox_open renders (ADR-0025)")
    llm = {m: [r for r in ctxs[m]["rules"] if r["src"] == LLM_POOL_SRC] for m in LLM_WEB_MODES}
    if any(v != llm[LLM_WEB_MODES[0]] for v in llm.values()):
        raise RenderError("the llm pool (key-holding agent-llm/catalog) must be identical in every LLM web mode")


_DENY_SAFE_PORTS = "http_access deny !Safe_ports"


def assert_rendered_order(text: str, ctx: Mapping[str, Any]) -> None:
    """Order invariants of the rendered text (build-env design §4.3): the four marked sandbox denies and one
    ``dst`` and one ``dst_loop`` floor line per sandbox rule precede ``deny !Safe_ports``; every line with a
    ``dst`` ACL names a source and a category earlier on the line, or is an allow line; ``denied_dst`` has
    exactly one more definition line (the public floor file); ``code_web_ok`` appears only on the pool_fetch
    ``all`` allow lines."""
    lines = text.splitlines()
    try:
        safe = lines.index(_DENY_SAFE_PORTS)
    except ValueError as exc:
        raise RenderError("rendered conf lacks deny !Safe_ports") from exc
    head = lines[:safe]
    marked = [
        "http_access deny port_sandbox notify_only mark_quiet",
        "http_access deny port_sandbox denied_domains mark_name",
        "http_access deny port_sandbox ip_literal mark_literal",
        "http_access deny port_sandbox code_pools code_denied mark_code",
    ]
    if any(m not in head for m in marked):
        raise RenderError("the marked sandbox denies must precede deny !Safe_ports")
    for r in ctx["sandbox_rules"]:
        for acl, mark in (("denied_dst", "mark_dst"), ("denied_dst_loop", "mark_dst_loop")):
            want = f"http_access deny port_sandbox {r['src']} {r['dst']} {acl} {mark}"
            if head.count(want) != 1 or lines.count(want) != 1:
                raise RenderError(f"sandbox rule {r} needs exactly one {acl} floor line ahead of the port checks")
    for line in lines:
        words = line.split()
        if not line.startswith("http_access ") or not ({"denied_dst", "denied_dst_loop"} & set(words)):
            continue
        if words[1] == "allow":
            continue
        first = min(words.index(a) for a in ("denied_dst", "denied_dst_loop") if a in words)
        src_ok = any(w in {"src_host", *(f"pool_{p['name']}" for p in ctx["pools"])} for w in words[:first])
        dst_ok = any(w == "all" or w.startswith("cat_") for w in words[:first])
        if not (src_ok and dst_ok):
            raise RenderError(f"a dst floor line must name a source and a category first: {line}")
    acl_lines = [ln for ln in lines if ln.startswith("acl denied_dst dst ")]
    if len(acl_lines) != 2 or acl_lines[1] != SQUID_PUBLIC_FILE_LINE:
        raise RenderError("denied_dst must have its inline line and exactly one public-floor file line")
    if lines.count(SQUID_CODE_WEB_LINE) != 1:
        raise RenderError("code_web_ok must be defined once, from the code-web file")
    code_web_allow = f"http_access allow port_sandbox {CODE_POOL_SRC} code_web_ok all "
    for line in lines:
        if " code_web_ok " in f"{line} " and not line.startswith(("acl ", code_web_allow)):
            raise RenderError(f"code_web_ok only on the pool_fetch web_public allow line: {line}")


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
    text = env.from_string(template).render(**ctx)
    assert_rendered_order(text, ctx)
    return text


def render_squid(
    bundle: ConfigBundle,
    snapshot_hash: str,
    ns: Sequence[str],
    template: str,
    hermes_login: bool = False,
    mode: EgressMode | None = None,
    host_cidrs: Sequence[str] = (),
) -> str:
    for server in ns:
        ipaddress.IPv4Address(server)
    check_mode_invariants(bundle, ns, hermes_login, host_cidrs)
    return render_context(squid_context(bundle, snapshot_hash, ns, hermes_login, mode, host_cidrs), template)


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
    host_cidrs: Sequence[str] | None = None,
) -> list[str]:
    """The live squid.conf must equal the render of its snapshot for the current egress mode and the live
    host addresses (``host_cidrs``: None = ``host_floor_cidrs()``; a moved host address outside the static
    floors is ``firewall_drift:host_floor_changed``, build-env design §4.1).

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
        hosts = host_cidrs if host_cidrs is not None else host_floor_cidrs(resolv_conf=resolv_conf)
        expected = render_squid(
            bundle, digest, nameservers(resolv_conf), template.read_text(encoding="utf-8"),
            hermes_login=login_window_flag.exists() and "\nacl cat_kiro_idc_login " in text, mode=mode,
            host_cidrs=hosts,
        )  # fmt: skip
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
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
    if _inline_denied_dst(expected) != _inline_denied_dst(text):
        # same snapshot, and the floors are identical in every mode: only the live host addresses moved
        return [f"firewall_drift:host_floor_changed {conf} names other host floor addresses than the live ones"]
    return [f"{tag} {conf} differs from the render for egress mode {mode}"]


def _inline_denied_dst(text: str) -> str | None:
    return next((ln for ln in text.splitlines() if ln.startswith("acl denied_dst dst ") and '"' not in ln), None)


# ------------------------------------------------------------------------------- firewall
@dataclass(frozen=True)
class Chain:
    family: str  # iptables | ip6tables
    name: str
    # None: the chain is created and filled but gets no jump of its own (its single reference is a
    # rule of another chain); check() then compares only its rules.
    parent: str | None
    rules: tuple[tuple[str, ...], ...]
    table: str = "filter"


# F1c (build-env design §7): a TCP path-MTU black hole between the uplink and Fastly (pypi.org,
# files.pythonhosted.org) drops full-size segments with no ICMP frag-needed. --set-mss only lowers
# a larger MSS (never raises one) and, unlike --clamp-mss-to-pmtu, works in every hook (V-K2).
# 1300 leaves margin under the largest tested good value (1360) for VPN/PPPoE paths.
MSS_CLAMP = "1300"
_MSS_SYN = ("-p", "tcp", "-m", "tcp", "--tcp-flags", "SYN,RST", "SYN")
_MSS_SET = ("-j", "TCPMSS", "--set-mss", MSS_CLAMP)
# FORWARD: squid's upstream SYN and the peer's SYN-ACK (every sandbox request, every mode);
# OUTPUT: the distro's own SYNs (what the peer sends us); INPUT: the peer's SYN-ACK to host sockets
# (what we send). One chain per parent: -o is meaningless in INPUT and -i in OUTPUT.
MSS_CHAINS = (
    Chain("iptables", "OSSBOT-MSS-FWD", "FORWARD", ((*_MSS_SYN, *_MSS_SET),), "mangle"),
    Chain("iptables", "OSSBOT-MSS-OUT", "OUTPUT", ((*_MSS_SYN, "!", "-o", "lo", *_MSS_SET),), "mangle"),
    Chain("iptables", "OSSBOT-MSS-IN", "INPUT", ((*_MSS_SYN, "!", "-i", "lo", *_MSS_SET),), "mangle"),
)


# Kernel floors (build-env design §4.4, ADR-0025). Every LOG rule is rate-limited per address (hashlimit) and
# rendered in the canonical form `iptables -S` prints; the prefixes use [A-Za-z0-9_-] only, so they are printed
# unquoted and the kernel writes `OSSBOT-FLOOR-IN=...`. ossbot-floorlog reads them: OSSBOT-FLOOR- = a sandbox
# packet to a host address (kind in), OSSBOT-PFLOOR- = the live proxy 10.212.0.2 to a floor (px, holds the
# running jobs), OSSBOT-TFLOOR- = another member of ossbot-egress, i.e. a root test proxy (tx, never holds).
BUILD_NET = "10.211.0.0/24"  # docker0: provision's image builds (no repo code)
ROOT_NETS = "10.214.0.0/16"  # Docker default-address-pools: any root-created network without a subnet
PUBFLOOR_CHAIN = "OSSBOT-PUBFLOOR"
_NEW = ("-m", "conntrack", "--ctstate", "NEW")


def _hashlimit(mode: str, name: str) -> tuple[str, ...]:
    return ("-m", "hashlimit", "--hashlimit-upto", "20/sec", "--hashlimit-burst", "40", "--hashlimit-mode", mode,
            "--hashlimit-name", name)  # fmt: skip


def _log(prefix: str) -> tuple[str, ...]:
    return ("-j", "LOG", "--log-prefix", prefix)


def floor_block(dst: str) -> tuple[tuple[str, ...], ...]:
    """The kernel floor for one IPv4 destination network: squid's upstream connections (the live proxy logged
    as PFLOOR, any other ossbot-egress member as TFLOOR), then the build and root-created networks (no LOG).
    Only NEW connections: return traffic of squid's public connections is ESTABLISHED."""
    d = str(ipaddress.IPv4Network(dst, strict=True))
    return (
        ("-s", f"{PROXY_IP}/32", "-d", d, *_NEW, *_hashlimit("dstip", "ossbot_floor_px"), *_log("OSSBOT-PFLOOR-")),
        ("-s", f"{PROXY_IP}/32", "-d", d, *_NEW, "-j", "DROP"),
        ("-s", EGRESS_NET, "-d", d, *_NEW, *_hashlimit("srcip,dstip", "ossbot_floor_tx"), *_log("OSSBOT-TFLOOR-")),
        ("-s", EGRESS_NET, "-d", d, *_NEW, "-j", "DROP"),
        ("-s", BUILD_NET, "-d", d, *_NEW, "-j", "DROP"),
        ("-s", ROOT_NETS, "-d", d, *_NEW, "-j", "DROP"),
    )


def pubfloor_rules(public: Iterable[str]) -> tuple[tuple[str, ...], ...]:
    """``OSSBOT-PUBFLOOR``: the floor block of every IPv4 entry of ``public.txt`` (IPv6 entries are squid-only:
    ip6tables OSSBOT-FWD drops everything on the bridges), in file order."""
    return tuple(r for p in public if ":" not in p for r in floor_block(p))


def firewall_plan(
    ns: str,
    uids: Mapping[str, str] | None = None,
    host_cidrs: Sequence[str] = (),
    public: Sequence[str] = (),
) -> dict[str, list[Chain]]:
    """Exactly the rules of DESIGN §11.4 (``uids`` maps account names to the value to render), plus the
    ADR-0025 kernel floors: ``host_cidrs`` the live host /32s outside the static floors (``host_floor_cidrs``),
    ``public`` the public-address floor of ``public.txt`` (``OSSBOT-PUBFLOOR``). No mode input: the floors
    are the same in every egress mode."""
    ipaddress.IPv4Address(ns)
    u = {name: (uids or {}).get(name, name) for name in SERVICE_USERS}
    sandbox_log = ("-m", "pkttype", "--pkt-type", "unicast", *_hashlimit("srcip", "ossbot_floor_in"),
                   *_log("OSSBOT-FLOOR-"))  # fmt: skip
    host_in = (
        ("-s", f"{PROXY_IP}/32", "-d", f"{ns}/32", "-p", "udp", "--dport", "53", "-j", "ACCEPT"),
        ("-s", f"{PROXY_IP}/32", "-d", f"{ns}/32", "-p", "tcp", "--dport", "53", "-j", "ACCEPT"),
        # (a) a sandbox packet at a host address (its /28's bridge address is the only one on-link): logged
        ("-s", TASK_NET, "-p", "tcp", "-m", "tcp", "--tcp-flags", "FIN,SYN,RST,ACK", "SYN", *sandbox_log),
        ("-s", TASK_NET, "-p", "udp", "-m", "udp", *sandbox_log),
        ("-s", TASK_NET, "-j", "DROP"),
        # (c) squid re-resolved into a distro-local address (INPUT, not FORWARD): split by source, logged
        ("-s", f"{PROXY_IP}/32", *_NEW, *_hashlimit("dstip", "ossbot_floor_pin"), *_log("OSSBOT-PFLOOR-")),
        ("-s", f"{PROXY_IP}/32", *_NEW, "-j", "DROP"),
        ("-s", EGRESS_NET, *_NEW, *_hashlimit("srcip,dstip", "ossbot_floor_tin"), *_log("OSSBOT-TFLOOR-")),
        ("-s", EGRESS_NET, "-m", "conntrack", "--ctstate", "NEW", "-j", "DROP"),
    )
    hosts = [str(ipaddress.IPv4Network(h, strict=True)) for h in host_cidrs]
    fwd = (
        ("-j", PUBFLOOR_CHAIN),
        *(r for d in (*IPV4_FLOOR_CIDRS, *hosts) for r in floor_block(d)),
        ("-s", TASK_NET, "!", "-d", TASK_NET, "-j", "DROP"),
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
            *MSS_CHAINS,
        ],
        "docker": [
            # created before OSSBOT-FWD's first rule jumps to it; no parent jump of its own (§4.4 item 0)
            Chain("iptables", PUBFLOOR_CHAIN, None, pubfloor_rules(public)),
            Chain("iptables", "OSSBOT-FWD", "DOCKER-USER", fwd),
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
        # filter chains keep the historical form byte for byte; other tables name theirs
        f = c.family if c.table == "filter" else f"{c.family} -t {c.table}"
        lines.append(f"{f} -N {c.name} 2>/dev/null || {f} -F {c.name}")
        lines.extend(f"{f} -A {c.name} {' '.join(rule)}" for rule in c.rules)
        if c.parent is not None:
            lines.append(f"{f} -C {c.parent} -j {c.name} 2>/dev/null || {f} -I {c.parent} 1 -j {c.name}")
    return "\n".join(lines) + "\n"


# Number of values each known option takes (build-env design §4.4 item 3). Rules are rendered in the
# forms `iptables -S` prints (iptables 1.8.10, V-I1); anything else is ("?", tok) = drift.
_ARITY: dict[str, int] = {
    "--tcp-flags": 2,
    **dict.fromkeys((
        "-s", "-d", "-i", "-o", "-p", "--dport", "--sport", "--uid-owner", "--ctstate", "-j", "-m",
        "--reject-with", "--set-mss", "--log-prefix", "--log-level", "--pkt-type",
        "--hashlimit-upto", "--hashlimit-burst", "--hashlimit-mode", "--hashlimit-name",
        "--hashlimit-htable-size", "--hashlimit-htable-max", "--hashlimit-htable-gcinterval",
        "--hashlimit-htable-expire", "--hashlimit-srcmask", "--hashlimit-dstmask",
    ), 1),
}  # fmt: skip
_IMPLICIT_MODULES = {"udp", "tcp"}
# Printed defaults dropped on both sides: `iptables -S` prints the REJECT default explicitly (verified
# live on iptables 1.8.10 nf_tables) and LOG's --log-level only when it is not warning (4).
_DEFAULTS: dict[str, frozenset[str]] = {
    "--reject-with": frozenset({"icmp-port-unreachable", "icmp6-port-unreachable"}),
    "--log-level": frozenset({"4", "warning"}),
}
# hashlimit table tuning (never what is dropped): compared only when the rendered rule sets it.
_HASHLIMIT_TUNING = frozenset({
    "--hashlimit-htable-size", "--hashlimit-htable-max", "--hashlimit-htable-gcinterval",
    "--hashlimit-htable-expire", "--hashlimit-srcmask", "--hashlimit-dstmask",
})  # fmt: skip


def _sorted_flags(value: str) -> str:
    return ",".join(sorted(value.split(",")))


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
        arity = _ARITY.get(tok)
        if arity is None or i + arity >= len(tokens):
            items.add(("?", tok))  # anything unexpected makes the rule differ (drift), never matches
            i += 1
            continue
        values = tokens[i + 1 : i + 1 + arity]
        i += 1 + arity
        if tok == "--tcp-flags":
            value = " ".join(_sorted_flags(v) for v in values)
        else:
            value = values[0]
        if (tok == "-m" and value in _IMPLICIT_MODULES) or value in _DEFAULTS.get(tok, frozenset()):
            negate = False
            continue
        if tok in {"-s", "-d"} and "/" not in value:
            value = f"{value}/32" if ":" not in value else f"{value}/128"
        items.add(((f"!{tok}" if negate else tok), value))
        negate = False
    return frozenset(items)


def _rule_matches(want: frozenset[tuple[str, str]], have: frozenset[tuple[str, str]]) -> bool:
    """``have`` (listed) equals ``want`` (rendered), ignoring hashlimit tuning ``want`` does not set."""
    set_by_want = {k for k, _ in want}
    return want == frozenset(kv for kv in have if kv[0] not in _HASHLIMIT_TUNING or kv[0] in set_by_want)


def _label(chain: Chain) -> str:
    return chain.family if chain.table == "filter" else f"{chain.family} -t {chain.table}"


def parse_listing(listing: str, chain: str, table: str = "filter") -> list[frozenset[tuple[str, str]]]:
    """Rules of ``chain`` from ``iptables [-t table] -S <chain>`` output, in order.

    Raises ValueError (naming the table and chain) for a line shlex cannot split."""
    out = []
    prefix = f"-A {chain} "
    for line in listing.splitlines():
        if line.startswith(prefix):
            try:
                tokens = shlex.split(line[len(prefix) :])
            except ValueError as exc:
                raise ValueError(f"{table} {chain}: unparseable listing line ({exc})") from exc
            out.append(normalize_rule(tokens))
    return out


def check_chain(chain: Chain, chain_listing: str, parent_listing: str) -> list[str]:
    problems: list[str] = []
    label = _label(chain)
    want = [normalize_rule(r) for r in chain.rules]
    try:
        have = parse_listing(chain_listing, chain.name, chain.table)
        parent_rules = [] if chain.parent is None else parse_listing(parent_listing, chain.parent, chain.table)
    except ValueError as exc:
        return [f"{chain.family} {exc}"]
    if len(have) != len(want) or not all(_rule_matches(w, h) for w, h in zip(want, have, strict=True)):
        problems.append(f"{label} {chain.name}: rules differ from the rendered set")
    if chain.parent is None:
        return problems
    jump = frozenset({("-j", chain.name)})
    if not parent_rules or parent_rules[0] != jump:
        problems.append(f"{label} {chain.parent}: jump to {chain.name} is not at position 1")
    if parent_rules.count(jump) != 1:
        problems.append(f"{label} {chain.parent}: expected exactly one jump to {chain.name}")
    return problems


def resolver_drift(chain_listing: str, ns: str) -> bool:
    """True when OSSBOT-IN accepts DNS for a different resolver than /etc/resolv.conf now names."""
    try:
        rules = parse_listing(chain_listing, "OSSBOT-IN")
    except ValueError:
        return False  # check_chain reports the unparseable listing as drift
    for rule in rules:
        dst = dict(rule).get("-d")
        if dict(rule).get("--dport") == "53" and dst is not None and dst != f"{ns}/32":
            return True
    return False


def _ipt(family: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run([family, "-w", *args], capture_output=True, text=True, check=check)


def apply(chains: Iterable[Chain]) -> None:
    for c in chains:
        t = ("-t", c.table)
        if _ipt(c.family, *t, "-N", c.name, check=False).returncode != 0:
            _ipt(c.family, *t, "-F", c.name)
        for rule in c.rules:
            _ipt(c.family, *t, "-A", c.name, *rule)
        if c.parent is None:
            continue
        while _ipt(c.family, *t, "-D", c.parent, "-j", c.name, check=False).returncode == 0:
            pass
        _ipt(c.family, *t, "-I", c.parent, "1", "-j", c.name)


def check(chains: Iterable[Chain], ns: str) -> list[str]:
    problems: list[str] = []
    for c in chains:
        t = ("-t", c.table)
        listing = _ipt(c.family, *t, "-S", c.name, check=False)
        parent = None if c.parent is None else _ipt(c.family, *t, "-S", c.parent, check=False)
        if listing.returncode != 0:
            problems.append(f"{_label(c)} {c.name}: chain missing")
            continue
        if c.name == "OSSBOT-IN" and resolver_drift(listing.stdout, ns):
            problems.append("firewall_drift:resolver_changed")
        parent_listing = parent.stdout if parent is not None and parent.returncode == 0 else ""
        found = check_chain(c, listing.stdout, parent_listing)
        if found and c.family == "iptables" and c.name == PUBFLOOR_CHAIN:
            # the live chain differs from public.txt (a partial pubip update, a hand edit): fail closed
            problems.append("firewall_drift:public_floor OSSBOT-PUBFLOOR differs from the public floor file")
        elif found and c.family == "iptables" and c.name == "OSSBOT-FWD" and host_floor_drift(c, listing.stdout):
            problems.append("firewall_drift:host_floor_changed the live host addresses moved (re-apply on start)")
        problems.extend(found)
    return problems


def _floor_hosts(rules: Iterable[frozenset[tuple[str, str]]]) -> set[str]:
    """The host /32s of the proxy floor in a chain: PFLOOR destinations outside the static floors."""
    static = {str(ipaddress.ip_network(c)) for c in IPV4_FLOOR_CIDRS}
    out = set()
    for rule in rules:
        d = dict(rule)
        if d.get("--log-prefix") == "OSSBOT-PFLOOR-" and d.get("-d") and d["-d"] not in static:
            out.add(d["-d"])
    return out


def host_floor_drift(chain: Chain, listing: str) -> bool:
    """True when the live OSSBOT-FWD names other host floor addresses than the rendered (live) list."""
    try:
        have = parse_listing(listing, chain.name)
    except ValueError:
        return False
    return _floor_hosts(have) != _floor_hosts(normalize_rule(r) for r in chain.rules)


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


def ensure_floor_files(floor_dir: Path = FLOOR_DIR) -> list[str]:
    """``render squid``: the public-floor directory and its sentinel files when missing (build-env design §4.7),
    so the proxy's ``ExecStartPre`` render always finds them and a fresh install starts *unknown* (no public web
    for repo code). Under the exclusive floor lock (C-M1); existing files are never touched."""
    created: list[str] = []
    with floor_lock(exclusive=True, timeout_s=WRITE_TIMEOUT_S, path=floor_dir / LOCK_PATH.name):
        for name, text in ((PUBLIC_FILE, public_text([])), (CODE_WEB_FILE, code_web_text(False))):
            path = floor_dir / name
            if not path.exists():
                _write_atomic(path, text)
                created.append(name)
    return created


def _touches_floor(chains: Iterable[Chain]) -> bool:
    return any(c.family == "iptables" and c.name in ("OSSBOT-FWD", PUBFLOOR_CHAIN) for c in chains)


def _firewall(args: argparse.Namespace) -> int:
    ns = nameservers(args.resolv_conf)[0]
    if args.print:
        if args.public_file is not None:  # the fixture capture: given floors, never the owner's address
            public, hosts = parse_public_text(args.public_file.read_text(encoding="utf-8")), list(args.host_cidr)
        else:
            public, hosts = read_public(args.floor_dir), list(host_floor_cidrs(resolv_conf=args.resolv_conf))
        print(render_script(select(firewall_plan(ns, None, hosts, public), args.part)), end="")
        return 0
    uids = resolve_uids()
    lock_path = args.floor_dir / LOCK_PATH.name

    def plan() -> list[Chain]:  # read public.txt and the host addresses under the lock
        hosts = host_floor_cidrs(resolv_conf=args.resolv_conf)
        return select(firewall_plan(ns, uids, hosts, read_public(args.floor_dir)), args.part)

    if args.apply:
        # C-M1: every part that touches OSSBOT-FWD/OSSBOT-PUBFLOOR holds the lock exclusively, so a concurrent
        # pubip update can't be flushed away by a refill from an older public.txt
        exclusive = _touches_floor(select(firewall_plan(ns), args.part))
        with floor_lock(exclusive=True, timeout_s=WRITE_TIMEOUT_S, path=lock_path) if exclusive else nullcontext():
            chains = plan()
            apply(chains)
        print(f"ossbot-firewall: applied {len(chains)} chains (resolver {ns})")
        return 0
    with floor_lock(exclusive=False, timeout_s=CHECK_TIMEOUT_S, path=lock_path):
        chains = plan()
        problems = check(chains, ns)
        if args.part in ("host", "all"):
            problems.extend(resolved_ipc_problems())
            problems.extend(io_uring_problems())
            problems.extend(squid_drift(resolv_conf=args.resolv_conf))
    for problem in problems:
        print(f"ossbot-firewall --check: {problem}", file=sys.stderr)
    return 1 if problems else 0


def _squid(args: argparse.Namespace) -> int:
    if args.mode is not None and args.out.resolve() == DEFAULT_SQUID_OUT.resolve():
        print(f"ossbot-render: --mode renders a test conf; refusing the live output {DEFAULT_SQUID_OUT}",
              file=sys.stderr)  # fmt: skip
        return 2
    digest, files = read_current(args.snapshot_root)
    bundle = parse_bundle(files)
    egress: EgressMode = args.mode or read_egress_mode(args.control_dir, bundle.egress.mode_default)
    hosts = host_floor_cidrs(resolv_conf=args.resolv_conf)
    text = render_squid(
        bundle, digest, nameservers(args.resolv_conf), args.template.read_text("utf-8"),
        args.hermes_login, egress, hosts,
    )  # fmt: skip
    created = ensure_floor_files(args.floor_dir)
    if created:
        print(f"created the public-floor sentinels {', '.join(created)} in {args.floor_dir} (unknown)")
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
    fw.add_argument("--floor-dir", type=Path, default=FLOOR_DIR)
    # --print only: render with these floors instead of the live ones (capture_firewall_fixture.sh)
    fw.add_argument("--public-file", type=Path, default=None)
    fw.add_argument("--host-cidr", action="append", default=[])
    sq = sub.add_parser("squid")
    sq.add_argument("--out", type=Path, default=DEFAULT_SQUID_OUT)
    sq.add_argument("--snapshot-root", type=Path, default=DEFAULT_SNAPSHOT_ROOT)
    sq.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    sq.add_argument("--resolv-conf", type=Path, default=Path("/etc/resolv.conf"))
    sq.add_argument("--hermes-login", action="store_true")
    # Default: the current egress-mode flag (absent = the snapshot's mode_default, unparseable = allowlist).
    # With --mode the output must not be the live conf (the floor-reach test renders test confs).
    sq.add_argument("--mode", choices=EGRESS_MODES, default=None)
    sq.add_argument("--control-dir", type=Path, default=DEFAULT_CONTROL_DIR)
    sq.add_argument("--floor-dir", type=Path, default=FLOOR_DIR)
    args = parser.parse_args(argv)
    if args.what == "firewall" and not args.print and (args.public_file is not None or args.host_cidr):
        parser.error("--public-file/--host-cidr only with --print")
    try:
        return _squid(args) if args.what == "squid" else _firewall(args)
    except (RenderError, OSError, ValueError, subprocess.CalledProcessError, subprocess.SubprocessError) as exc:
        print(f"ossbot-render: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
