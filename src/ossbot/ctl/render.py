"""Renderers for the egress layer (DESIGN §11.2-11.4): squid.conf and the ossbot-firewall chains.

squid: from the config snapshot's ``egress.yaml`` (+ ``profile.kiro`` for ``kiro_idc_login``,
``targets.yaml`` ``egress_extra`` for ``pkg_helm``), ``dns_nameservers`` from ``/etc/resolv.conf``.
Sandboxes are classed by source pool (§10.3); profiles sharing a pool must have identical
categories, otherwise rendering fails (squid could not tell them apart).

firewall: ``OSSBOT-IN``/``OSSBOT-OUT`` (+ ip6tables) for ``ossbot-firewall.service`` (part
``host``) and ``OSSBOT-FWD`` in ``DOCKER-USER`` for ``ossbot-firewall-docker.service`` (part
``docker``). ``--apply`` flushes and refills each chain and leaves exactly one jump at position
1 of its parent; ``--check`` compares chain contents (order included) and jump positions and
exits 1 on any drift, including a changed resolver (``firewall_drift:resolver_changed``).

Usage (root): ``python -I -m ossbot.ctl.render firewall --apply|--check|--print [--part P]`` and
``python -I -m ossbot.ctl.render squid [--out PATH] [--hermes-login]``.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import pwd
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import jinja2

from ossbot.common.config import ConfigBundle, EgressConfig, ProfileConfig, TargetsConfig, parse_bundle
from ossbot.common.snapshot import read_current

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
HOST_CALLER_ORDER = ("runner", "hermes", "gatekeeper")
SERVICE_USERS = ("ossrunner", "hermes", "gatekeeper", "ossbox", "ossnobody")
DEFAULT_TEMPLATE = Path("/opt/ossbot/app/sandbox/proxy/squid.conf.j2")
DEFAULT_SQUID_OUT = Path("/etc/ossbot/proxy/squid.conf")
DEFAULT_SNAPSHOT_ROOT = Path("/srv/ossbot/config-snapshot")


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


def helm_hosts(targets: TargetsConfig) -> list[str]:
    hosts = list(targets.defaults.egress_extra)
    for category in targets.categories:
        for repo in category.repos:
            hosts.extend(repo.egress_extra)
    return hosts


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


def squid_context(
    bundle: ConfigBundle, snapshot_hash: str, ns: Sequence[str], hermes_login: bool = False
) -> dict[str, Any]:
    egress = bundle.egress
    cats = resolve_categories(egress, bundle.profile, bundle.targets, hermes_login)
    callers = egress.host_callers
    listeners = [{"bind": str(SANDBOX_PORT), "name": "sandbox"}]
    listeners += [{"bind": f"{PROXY_IP}:{callers[c].port}", "name": c} for c in HOST_CALLER_ORDER if c in callers]
    rules: list[dict[str, str]] = []
    pools = []
    for pool, cidr, profiles in SQUID_POOLS:
        lists = [egress.sandbox_profiles.get(p) for p in profiles]  # type: ignore[call-overload]
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
    for caller in HOST_CALLER_ORDER:
        if caller not in callers:
            continue
        for category in callers[caller].categories:  # type: ignore[index]
            dst = _dst(category, cats)
            if dst is not None:
                rules.append({"port": caller, "src": "src_host", "dst": dst})
    referenced = {r["dst"][4:] for r in rules if r["dst"].startswith("cat_")}
    return {
        "snapshot_hash": snapshot_hash,
        "listeners": listeners,
        "nameservers": list(ns),
        "host_src": HOST_BRIDGE,
        "pools": pools,
        "categories": {k: v for k, v in cats.items() if k in referenced},
        "denied_domains": _dedupe(egress.always_denied.domains),
        "denied_cidrs": list(egress.always_denied.cidrs),
        "rules": rules,
    }


def render_squid(
    bundle: ConfigBundle, snapshot_hash: str, ns: Sequence[str], template: str, hermes_login: bool = False
) -> str:
    for server in ns:
        ipaddress.IPv4Address(server)  # never render anything but literal IPv4 addresses
    # Not HTML: every value is validated (HOST_RE hosts, literal IPs, fixed names), so no escaping.
    env = jinja2.Environment(
        autoescape=False,  # noqa: S701
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )
    return env.from_string(template).render(**squid_context(bundle, snapshot_hash, ns, hermes_login))


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
    out: list[tuple[str, ...]] = []
    for user, port in (("ossrunner", 3140), ("hermes", 3141), ("gatekeeper", 3150)):
        owner = ("-m", "owner", "--uid-owner", u[user])
        out.append((*owner, "-d", PROXY_IP, "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"))
        out.append((*owner, "-o", "lo", "-j", "ACCEPT"))
        out.append((*owner, "-j", "REJECT"))
    for user in ("ossbox", "ossnobody"):
        owner = ("-m", "owner", "--uid-owner", u[user])
        out.append((*owner, "-o", "lo", "-j", "ACCEPT"))
        out.append((*owner, "-j", "REJECT"))
    out6: list[tuple[str, ...]] = []
    for user in SERVICE_USERS:
        owner = ("-m", "owner", "--uid-owner", u[user])
        out6.append((*owner, "-o", "lo", "-j", "ACCEPT"))
        out6.append((*owner, "-j", "DROP"))
    fwd6 = (
        ("-i", "docker0", "-j", "DROP"), ("-o", "docker0", "-j", "DROP"),
        ("-i", "br-+", "-j", "DROP"), ("-o", "br-+", "-j", "DROP"),
    )  # fmt: skip
    return {
        "host": [
            Chain("iptables", "OSSBOT-IN", "INPUT", host_in),
            Chain("iptables", "OSSBOT-OUT", "OUTPUT", tuple(out)),
            Chain("ip6tables", "OSSBOT-OUT", "OUTPUT", tuple(out6)),
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
    args = parser.parse_args(argv)
    try:
        if args.what == "squid":
            digest, files = read_current(args.snapshot_root)
            text = render_squid(
                parse_bundle(files), digest, nameservers(args.resolv_conf), args.template.read_text("utf-8"),
                args.hermes_login,
            )  # fmt: skip
            _write_atomic(args.out, text)
            print(f"rendered {args.out} from snapshot {digest}")
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
        for problem in problems:
            print(f"ossbot-firewall --check: {problem}", file=sys.stderr)
        return 1 if problems else 0
    except (RenderError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"ossbot-render: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
