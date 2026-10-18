"""Egress renderers (DESIGN §11.2-11.4): golden squid.conf and iptables renders, --check drift."""

from __future__ import annotations

import os
import re
import socket
from pathlib import Path

import pytest

from ossbot.common.config import parse_bundle
from ossbot.ctl import render

ROOT = Path(__file__).resolve().parents[3]
GOLDEN = Path(__file__).parent / "golden"
NS = "10.255.255.254"
TEMPLATE = (ROOT / "sandbox" / "proxy" / "squid.conf.j2").read_text(encoding="utf-8")
REGEN = os.environ.get("OSSBOT_REGEN_GOLDEN") == "1"


def _bundle(extra_targets: str = "") -> render.ConfigBundle:
    files = {n: (ROOT / "config" / n).read_bytes() for n in ("policy.yaml", "egress.yaml", "targets.yaml")}
    files["profile.yaml"] = (ROOT / "tests" / "fixtures" / "profile.test.yaml").read_bytes()
    if extra_targets:
        files["targets.yaml"] = files["targets.yaml"].replace(b"  egress_extra: []", extra_targets.encode(), 1)
    return parse_bundle(files)


def _golden(name: str, text: str) -> str:
    path = GOLDEN / name
    if REGEN:
        path.write_text(text, encoding="utf-8", newline="\n")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------- iptables
def test_iptables_render_matches_golden() -> None:
    text = render.render_script(render.select(render.firewall_plan(NS), "all"))
    assert text == _golden("iptables.rules", text)


def _design_rules() -> list[str]:
    """Every iptables command line of the two §11.4 code blocks, with $NS substituted."""
    design = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    section = design[design.index("### 11.4 iptables") : design.index("## 12. Gatekeeper")]
    lines = []
    for block in re.findall(r"```\n(.*?)```", section, re.DOTALL):
        for line in block.splitlines():
            if line.startswith("iptables "):
                lines.append(re.sub(r"\s+", " ", line.replace("$NS", NS)).strip())
    return lines


def test_golden_contains_design_section_11_4_in_order() -> None:
    golden = (GOLDEN / "iptables.rules").read_text(encoding="utf-8").splitlines()
    design = _design_rules()
    assert len(design) == 34
    positions = [golden.index(line) for line in design]  # ValueError if any design rule is missing
    assert positions == sorted(positions)
    # the only additions are the ip6tables chains (DESIGN: "ip6tables ... DROP for the same uids")
    extra = [line for line in golden if line not in design]
    assert extra and all(line.startswith("ip6tables ") for line in extra)


def test_parts_split_host_and_docker() -> None:
    plan = render.firewall_plan(NS)
    assert [(c.family, c.name, c.parent) for c in plan["host"]] == [
        ("iptables", "OSSBOT-IN", "INPUT"),
        ("iptables", "OSSBOT-OUT", "OUTPUT"),
        ("ip6tables", "OSSBOT-OUT", "OUTPUT"),
        ("ip6tables", "OSSBOT-IN", "INPUT"),
        ("ip6tables", "OSSBOT-FWD", "FORWARD"),
    ]
    assert [(c.family, c.name, c.parent) for c in plan["docker"]] == [("iptables", "OSSBOT-FWD", "DOCKER-USER")]


def test_dns_accepts_precede_the_drops() -> None:
    rules = render.firewall_plan(NS)["host"][0].rules
    assert [r[-1] for r in rules] == ["ACCEPT", "ACCEPT", "DROP", "DROP"]


def test_every_service_uid_refuses_dns_before_the_lo_accept() -> None:
    """P1-1: the WSL resolver sits on lo, so port 53 must be refused before ``-o lo -j ACCEPT``."""
    host = render.firewall_plan(NS)["host"]
    for chain, verdict in ((host[1], "REJECT"), (host[2], "DROP")):
        for user in render.SERVICE_USERS:
            rules = [r for r in chain.rules if r[3] == user]
            lo = rules.index(("-m", "owner", "--uid-owner", user, "-o", "lo", "-j", "ACCEPT"))
            for proto in ("udp", "tcp"):
                dns = ("-m", "owner", "--uid-owner", user, "-p", proto, "--dport", "53", "-j", verdict)
                assert rules.index(dns) < lo, (chain.family, user, proto)
            assert all("ACCEPT" not in r for r in rules[: rules.index(dns)]), (chain.family, user)


def test_bad_nameserver_refused() -> None:
    with pytest.raises(ValueError):
        render.firewall_plan("10.0.0.1; iptables -F")


def _listing(chain: render.Chain, uid_map: dict[str, str]) -> str:
    """What `iptables -S <chain>` prints for a correctly applied chain (numeric uids, -m udp/tcp)."""
    out = [f"-N {chain.name}"]
    for rule in chain.rules:
        tokens = list(rule)
        for i, tok in enumerate(tokens):
            if tok == "--uid-owner":
                tokens[i + 1] = uid_map.get(tokens[i + 1], tokens[i + 1])
        if "-p" in tokens:
            proto = tokens[tokens.index("-p") + 1]
            tokens.insert(tokens.index("-p") + 2, f"-m {proto}")
        if tokens[-2:] == ["-j", "REJECT"]:  # iptables -S prints the default explicitly (live-verified)
            tokens += ["--reject-with", "icmp-port-unreachable"]
        text = " ".join(tokens)
        text = re.sub(r"-d (\d+\.\d+\.\d+\.\d+) ", r"-d \1/32 ", text)
        out.append(f"-A {chain.name} {text}")
    return "\n".join(out) + "\n"


UIDS = {"ossrunner": "990", "hermes": "991", "gatekeeper": "992", "ossbox": "993", "ossnobody": "1001"}


def test_check_accepts_iptables_save_format() -> None:
    for chain in render.select(render.firewall_plan(NS, UIDS), "all"):
        parent = f"-P {chain.parent} ACCEPT\n-A {chain.parent} -j {chain.name}\n-A {chain.parent} -j OTHER\n"
        assert render.check_chain(chain, _listing(chain, UIDS), parent) == [], chain.name


def test_check_detects_drift() -> None:
    chain = render.firewall_plan(NS, UIDS)["host"][1]
    good = _listing(chain, UIDS)
    parent_ok = f"-A OUTPUT -j {chain.name}\n"
    flushed = f"-N {chain.name}\n"
    assert render.check_chain(chain, flushed, parent_ok)
    reordered = "\n".join([good.splitlines()[0], *reversed(good.splitlines()[1:])]) + "\n"
    assert render.check_chain(chain, reordered, parent_ok)
    assert render.check_chain(chain, good, f"-A OUTPUT -j DOCKER\n-A OUTPUT -j {chain.name}\n")  # not position 1
    assert render.check_chain(chain, good, f"-A OUTPUT -j {chain.name}\n-A OUTPUT -j {chain.name}\n")  # two jumps
    assert render.check_chain(chain, good, "")  # jump missing
    extra = good + f"-A {chain.name} -j ACCEPT\n"
    assert render.check_chain(chain, extra, parent_ok)
    weakened = good.replace("--reject-with icmp-port-unreachable", "--reject-with tcp-reset", 1)
    assert render.check_chain(chain, weakened, parent_ok)
    unknown = good.replace(" -j REJECT", " -m comment --comment x -j REJECT", 1)
    assert render.check_chain(chain, unknown, parent_ok)


def test_resolver_change_detected() -> None:
    old = render.firewall_plan("10.255.255.1", UIDS)["host"][0]
    listing = _listing(old, UIDS)
    assert render.resolver_drift(listing, NS)
    assert not render.resolver_drift(_listing(render.firewall_plan(NS, UIDS)["host"][0], UIDS), NS)
    new = render.firewall_plan(NS, UIDS)["host"][0]
    assert render.check_chain(new, listing, "-A INPUT -j OSSBOT-IN\n")


def test_resolved_ipc_check_requires_mask_and_closed_varlink(tmp_path: Path) -> None:
    """P1-6: systemd-resolved is a DNS channel past OSSBOT-OUT unless masked and not listening."""
    link, sock_path = tmp_path / "systemd-resolved.service", tmp_path / "vl"
    problems = render.resolved_ipc_problems(link, sock_path)
    assert problems == ["firewall_drift:resolved_ipc systemd-resolved is not masked"]
    link.write_text("[Unit]\n")  # a real unit file (unmasked override) is not a mask
    assert render.resolved_ipc_problems(link, sock_path) == problems
    link.unlink()
    link.symlink_to("/dev/null")
    assert render.resolved_ipc_problems(link, sock_path) == []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(sock_path))
        listener.listen(1)
        [problem] = render.resolved_ipc_problems(link, sock_path)
    assert "resolved_ipc" in problem and "varlink" in problem
    assert render.resolved_ipc_problems(link, sock_path) == []  # a stale socket file without listener


@pytest.mark.parametrize(("value", "ok"), [("2\n", True), ("0\n", False), ("1\n", False), (None, False)])
def test_io_uring_check_requires_disabled_2(tmp_path: Path, value: str | None, ok: bool) -> None:
    """P1-14: IORING_OP_SOCKET bypasses RestrictAddressFamilies, so io_uring must be off for every
    process (1 still allows kernel.io_uring_group); a kernel without the sysctl fails closed."""
    sysctl = tmp_path / "io_uring_disabled"
    if value is not None:
        sysctl.write_text(value)
    problems = render.io_uring_problems(sysctl)
    if ok:
        assert problems == []
    else:
        [problem] = problems
        assert problem.startswith("firewall_drift:io_uring ") and "expected 2" in problem


def test_nameservers_from_resolv_conf(tmp_path: Path) -> None:
    conf = tmp_path / "resolv.conf"
    conf.write_text("# generated by WSL\nnameserver 10.255.255.254\nnameserver ::1\nsearch .\n")
    assert render.nameservers(conf) == [NS]
    conf.write_text("nameserver ::1\n")
    with pytest.raises(render.RenderError):
        render.nameservers(conf)


# ------------------------------------------------------------------------------ squid
def test_squid_render_matches_golden() -> None:
    text = render.render_squid(_bundle(), "0" * 64, [NS], TEMPLATE)
    assert text == _golden("squid.conf", text)


def test_squid_listeners_and_classes() -> None:
    text = render.render_squid(_bundle(), "0" * 64, [NS], TEMPLATE)
    assert "http_port 3128 name=sandbox\n" in text
    for port, name in ((3140, "runner"), (3141, "hermes"), (3150, "gatekeeper")):
        assert f"http_port 10.212.0.2:{port} name={name}\n" in text
    assert f"dns_nameservers {NS}\n" in text
    assert "http_access deny port_sandbox src_host\n" in text
    assert text.rstrip().endswith("http_access deny all")
    # research and hermes get web_public (= all) but only after every deny
    allow_all = [i for i, line in enumerate(text.splitlines()) if " all " in line and "allow" in line]
    last_deny = max(i for i, line in enumerate(text.splitlines()) if line.startswith("http_access deny port_sandbox"))
    assert allow_all and min(allow_all) > last_deny
    assert "http_access allow port_sandbox pool_research all !denied_dst\n" in text
    assert "http_access allow port_hermes src_host all !denied_dst\n" in text
    assert "http_access allow port_sandbox pool_llm cat_kiro_core !denied_dst\n" in text
    assert "pool_llm all" not in text and "pool_fetch all" not in text
    assert "http_access allow port_gatekeeper src_host cat_gatekeeper !denied_dst\n" in text
    assert "port_gatekeeper src_host all" not in text and "port_runner src_host all" not in text


def test_squid_denied_by_default_and_inactive_categories_absent() -> None:
    text = render.render_squid(_bundle(), "0" * 64, [NS], TEMPLATE)
    assert "telemetry" not in text  # kiro_telemetry is denied_by_default
    assert "cat_kiro_idc_login" not in text  # only while `ossctl hermes login` runs
    assert "cat_pkg_helm" not in text  # no egress_extra in targets.yaml


def test_squid_hermes_login_window_adds_idc_hosts() -> None:
    text = render.render_squid(_bundle(), "0" * 64, [NS], TEMPLATE, hermes_login=True)
    assert "acl cat_kiro_idc_login dstdomain -n test-org.awsapps.com oidc.us-east-1.amazonaws.com" in text
    assert "http_access allow port_hermes src_host cat_kiro_idc_login !denied_dst\n" in text


def test_squid_pkg_helm_from_targets_snapshot() -> None:
    text = render.render_squid(_bundle("  egress_extra: [charts.jetstack.io]"), "0" * 64, [NS], TEMPLATE)
    assert "acl cat_pkg_helm dstdomain -n charts.jetstack.io\n" in text
    assert "http_access allow port_sandbox pool_fetch cat_pkg_helm !denied_dst\n" in text


def test_squid_refuses_pool_sharing_profiles_with_different_categories() -> None:
    bundle = _bundle()
    egress = bundle.egress.model_copy(
        update={"sandbox_profiles": {**bundle.egress.sandbox_profiles, "validate": ["github_read"]}}
    )
    with pytest.raises(render.RenderError):
        render.squid_context(render.ConfigBundle(bundle.policy, bundle.targets, egress, bundle.profile), "x", [NS])


def test_squid_refuses_non_ip_nameserver() -> None:
    with pytest.raises(ValueError):
        render.render_squid(_bundle(), "0" * 64, ["evil.example"], TEMPLATE)


# Phase 1 review P1-11 / P1-12 (ADR-0005 addendum): squid must never resolve a name, forward or
# reverse, that the caller may not reach. Squid evaluates one http_access line left to right and
# stops at the first mismatch, and only "slow" ACLs (dst, or a name ACL without -n) cause lookups.
_SLOW_TYPES = {"dst", "dst_as", "srcdomain", "srcdom_regex"}
_NAME_TYPES = {"dstdomain", "dstdom_regex"}


def _acls(text: str) -> dict[str, list[str]]:
    """ACL name -> [type, args...] of the rendered config."""
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        if line.startswith("acl "):
            _, name, *rest = line.split()
            assert name not in out, f"acl {name} defined twice"
            out[name] = rest
    return out


def _lookup_acls(acls: dict[str, list[str]]) -> set[str]:
    """ACLs whose evaluation can send a DNS query."""
    slow = set()
    for name, (kind, *args) in acls.items():
        if kind in _SLOW_TYPES or (kind in _NAME_TYPES and "-n" not in args[:2]):
            slow.add(name)
    return slow


def _access(text: str) -> list[tuple[str, list[str]]]:
    return [(line.split()[1], line.split()[2:]) for line in text.splitlines() if line.startswith("http_access ")]


@pytest.mark.parametrize("hermes_login", [False, True])
def test_squid_name_acls_never_reverse_lookup(hermes_login: bool) -> None:
    """P1-11: every dstdomain/dstdom_regex carries -n, so an IP-literal request is never matched
    through its PTR name; the only lookup ACL left is the resolved-IP deny."""
    text = render.render_squid(_bundle("  egress_extra: [charts.jetstack.io]"), "0" * 64, [NS], TEMPLATE,
                               hermes_login=hermes_login)  # fmt: skip
    acls = _acls(text)
    names = {n: a for n, a in acls.items() if a[0] in _NAME_TYPES}
    assert {n for n in names if n.startswith("cat_")} >= {"cat_kiro_core", "cat_github_read", "cat_pkg_helm"}
    assert {"denied_domains", "ip_literal"} <= set(names)
    for name, (_kind, *args) in names.items():
        assert args[0] == "-n", name
    assert _lookup_acls(acls) == {"denied_dst"}
    assert acls["denied_dst"][0] == "dst"


@pytest.mark.parametrize("hermes_login", [False, True])
def test_squid_no_lookup_before_a_category_match(hermes_login: bool) -> None:
    """P1-12: no deny line uses a lookup ACL, and on every allow line the resolved-IP deny comes
    after the port, the source and the category, so only allowed names are ever resolved."""
    text = render.render_squid(_bundle(), "0" * 64, [NS], TEMPLATE, hermes_login=hermes_login)
    acls = _acls(text)
    slow = _lookup_acls(acls)
    lines = _access(text)
    assert lines[-1] == ("deny", ["all"])
    allows = [tokens for verdict, tokens in lines if verdict == "allow"]
    assert allows
    for verdict, tokens in lines:
        if verdict == "deny":
            assert not {t.lstrip("!") for t in tokens} & slow, tokens
    for tokens in allows:
        port, src, dst, *rest = tokens
        assert port.startswith("port_") and (src == "src_host" or src.startswith("pool_")), tokens
        assert dst == "all" or dst.startswith("cat_"), tokens
        assert rest == ["!denied_dst"], tokens  # DESIGN §11.3: resolved-IP deny for every allowed name
    first_allow = next(i for i, (verdict, _) in enumerate(lines) if verdict == "allow")
    assert ("deny", ["ip_literal"]) in lines[:first_allow]
    # round 5 NIT: cache manager requests never reach the allow lines (web_public callers' `all`)
    assert ("deny", ["manager"]) in lines[:first_allow]
    assert acls["manager"] == ["url_regex", "-i", "^cache_object://", "/squid-internal-mgr/"]


_LITERALS = [
    "203.0.113.7", "203.0.113.7.", "3405803783", "0xcb007107", "0xCB.0.0x71.07", "0313.0.0161.07", "127.1",
    "10.1", "0", "::1", "2001:db8::7", "[2001:db8::7]", "::ffff:10.0.0.1", "fe80::1%eth0",
]  # fmt: skip
_NAMES = [
    "api.github.com", "1password.com", "123.com", "0x00.net", "cafe.de", "1e100.net", "a.b", "100.64.example",
    "runtime.us-east-1.kiro.dev", "10.in-addr.arpa",
]  # fmt: skip


def test_ip_literal_patterns_match_literals_only() -> None:
    """squid matches dstdom_regex (POSIX ERE, -i) against the request host; Python re agrees on
    these patterns. Every inet_aton/getaddrinfo numeric form is caught, no DNS name is."""
    pats = [re.compile(p, re.IGNORECASE) for p in render.IP_LITERAL_PATTERNS]
    for host in _LITERALS:
        assert any(p.search(host) for p in pats), host
    for host in _NAMES:
        assert not any(p.search(host) for p in pats), host


def test_squid_pinger_off_and_cidrs_validated() -> None:
    text = render.render_squid(_bundle(), "0" * 64, [NS], TEMPLATE)
    assert "pinger_enable off\n" in text  # no ICMP helper (it could not open a raw socket anyway)
    assert "0.0.0.0/8" in _acls(text)["denied_dst"]  # a name resolving to 0.0.0.0 reaches squid's own lo
    # fake-IP range of Windows VPN/proxy clients (via the WSL NAT), IETF, multicast, reserved
    for cidr in ("198.18.0.0/15", "192.0.0.0/24", "224.0.0.0/4", "240.0.0.0/4"):
        assert cidr in _acls(text)["denied_dst"], cidr
    bundle = _bundle()
    denied = bundle.egress.always_denied.model_copy(update={"cidrs": ["10.0.0.0/8 all"]})
    egress = bundle.egress.model_copy(update={"always_denied": denied})
    with pytest.raises(ValueError):
        render.render_squid(render.ConfigBundle(bundle.policy, bundle.targets, egress, bundle.profile), "x", [NS],
                            TEMPLATE)  # fmt: skip
