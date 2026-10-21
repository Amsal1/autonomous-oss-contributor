"""Egress renderers (DESIGN §11.2-11.4): golden squid.conf and iptables renders, --check drift."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import time
from pathlib import Path

import pytest

from ossbot.common.config import ConfigError, EgressConfig, parse_bundle, parse_yaml_model
from ossbot.common.control_flags import write_flag
from ossbot.common.snapshot import write_snapshot
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
MODES = ("allowlist", "research", "open", "sandbox_open")


@pytest.mark.parametrize("mode", MODES)
def test_squid_render_matches_golden(mode: str) -> None:
    text = render.render_squid(_bundle(), "0" * 64, [NS], TEMPLATE, mode=mode)  # type: ignore[arg-type]
    assert text == _golden(f"squid.{mode}.conf", text)
    assert f"# Egress mode: {mode} " in text


def test_default_mode_is_open() -> None:
    bundle = _bundle()
    assert bundle.egress.mode_default == "open"
    assert render.render_squid(bundle, "0" * 64, [NS], TEMPLATE) == render.render_squid(
        bundle, "0" * 64, [NS], TEMPLATE, mode="open"
    )


def _allows(text: str) -> set[str]:
    return {line for line in text.splitlines() if line.startswith("http_access allow ")}


def test_mode_table_of_design_11_5() -> None:
    """allowlist: no web_public anywhere; research: + web_public for research and hermes; open: + the
    llm pool (agent-llm and catalog share it). github_user_content for research/hermes in every mode."""
    texts = {m: render.render_squid(_bundle(), "0" * 64, [NS], TEMPLATE, mode=m) for m in MODES}  # type: ignore[arg-type]
    web = {m: {line for line in _allows(t) if " all " in line} for m, t in texts.items()}
    assert web["allowlist"] == set()
    assert web["research"] == {
        "http_access allow port_sandbox pool_research all !denied_dst",
        "http_access allow port_hermes src_host all !denied_dst",
    }
    assert web["open"] == web["research"] | {"http_access allow port_sandbox pool_llm all !denied_dst"}
    assert web["sandbox_open"] == web["open"] | {"http_access allow port_sandbox pool_fetch all !denied_dst"}
    for text in texts.values():
        acl = "acl cat_github_user_content dstdomain -n .github.io gist.github.com gist.githubusercontent.com\n"
        assert acl in text
        assert "http_access allow port_sandbox pool_research cat_github_user_content !denied_dst\n" in text
        assert "http_access allow port_hermes src_host cat_github_user_content !denied_dst\n" in text
        for line in _allows(text):
            if "pool_fetch" in line or "port_runner" in line or "port_gatekeeper" in line or "pool_llm" in line:
                assert "github_user_content" not in line, line


def test_invariants_identical_in_every_mode() -> None:
    """Everything but the research/hermes/llm grants and sandbox_open's one pool_fetch ``all`` line is
    byte-identical between the modes."""
    texts = {m: render.render_squid(_bundle(), "0" * 64, [NS], TEMPLATE, mode=m) for m in MODES}  # type: ignore[arg-type]

    def fixed(text: str) -> list[str]:
        return [
            line for line in text.splitlines()
            if not line.startswith("# Egress mode:")
            and not (line.startswith("http_access allow ") and any(
                k in line for k in ("pool_research", "port_hermes", "pool_llm", "pool_fetch all ")))
        ]  # fmt: skip

    assert fixed(texts["allowlist"]) == fixed(texts["research"]) == fixed(texts["open"]) == fixed(texts["sandbox_open"])
    denied = _acls(texts["open"])["denied_domains"][2:]
    assert {"uploads.github.com", "checkpoint-api.hashicorp.com", "telegra.ph", "api.telegra.ph",
            "edit.telegra.ph"} == set(denied)  # fmt: skip
    assert ".github.io" not in denied and "gist.github.com" not in denied
    assert "::/128" in _acls(texts["open"])["denied_dst"]  # P1-15


def _with_telegraph(bundle: render.ConfigBundle, enabled: bool) -> render.ConfigBundle:
    tg = bundle.telegram.model_copy(
        update={"telegraph": bundle.telegram.telegraph.model_copy(update={"enabled": enabled})}
    )
    return render.ConfigBundle(bundle.policy, bundle.targets, bundle.egress, bundle.profile, tg)


@pytest.mark.parametrize("mode", MODES)
def test_telegraph_category_only_for_the_gatekeeper_and_only_when_enabled(mode: str) -> None:
    """AC 28 (§14.12.8): off -> no telegraph ACL at all; on -> one gatekeeper allow ahead of the denies,
    and telegra.ph stays in always_denied for every other caller in every mode."""

    def squid(bundle: render.ConfigBundle) -> str:
        return render.render_squid(bundle, "0" * 64, [NS], TEMPLATE, mode=mode)  # type: ignore[arg-type]

    off = squid(_with_telegraph(_bundle(), False))
    assert "cat_telegraph" not in off
    assert off == squid(_bundle())
    on = squid(_with_telegraph(_bundle(), True))
    assert _acls(on)["cat_telegraph"][2:] == ["api.telegra.ph"]
    lines = on.splitlines()
    uses = [ln for ln in lines if "cat_telegraph" in ln and ln.startswith("http_access")]
    assert uses == ["http_access allow port_gatekeeper src_host cat_telegraph !ip_literal !denied_dst"]
    assert lines.index(uses[0]) < lines.index("http_access deny denied_domains")
    assert {"telegra.ph", "api.telegra.ph", "edit.telegra.ph"} <= set(_acls(on)["denied_domains"][2:])


def test_the_telegraph_category_cannot_be_listed_by_anyone() -> None:
    text = (ROOT / "config" / "egress.yaml").read_text(encoding="utf-8")
    for bad in (text.replace("  research: [kiro_core, github_user_content]", "  research: [kiro_core, telegraph]"),
                text.replace("categories: [gatekeeper]}", "categories: [gatekeeper, telegraph]}"),
                text.replace("  allowlist: {}", "  allowlist: {hermes: [telegraph]}")):  # fmt: skip
        assert bad != text
        with pytest.raises(ConfigError, match="telegraph"):
            parse_yaml_model(EgressConfig, "egress.yaml", bad.encode())


def test_grant_to_a_code_running_profile_is_rejected_by_the_schema() -> None:
    """Decision B3: grantee keys are a closed Literal set (runner/gatekeeper are not keys); fetch and
    validate are keys for sandbox_open only (ADR-0023), refused in every other mode."""
    text = (ROOT / "config" / "egress.yaml").read_text(encoding="utf-8")
    for grantee in ("fetch", "validate", "runner", "gatekeeper"):
        bad = text.replace("  allowlist: {}", f"  allowlist: {{{grantee}: [web_public]}}")
        with pytest.raises(ConfigError, match=grantee):
            parse_yaml_model(EgressConfig, "egress.yaml", bad.encode())
    with pytest.raises(ConfigError, match="every egress mode"):
        parse_yaml_model(EgressConfig, "egress.yaml", text.replace("  allowlist: {}\n", "").encode())


def _with_egress(bundle: render.ConfigBundle, **update: object) -> render.ConfigBundle:
    egress = bundle.egress.model_copy(update=update)
    return render.ConfigBundle(bundle.policy, bundle.targets, egress, bundle.profile)


def test_renderer_refuses_web_public_in_base_lists_of_code_or_trusted_callers() -> None:
    """Data that bypasses the grant keys (a base list) still cannot give fetch/runner/gatekeeper the web."""
    bundle = _bundle()
    profiles = dict(bundle.egress.sandbox_profiles)
    profiles["fetch"] = [*profiles["fetch"], "web_public"]
    profiles["validate"] = [*profiles["validate"], "web_public"]
    with pytest.raises(render.RenderError, match="fetch/validate"):
        render.render_squid(_with_egress(bundle, sandbox_profiles=profiles), "0" * 64, [NS], TEMPLATE)
    callers = dict(bundle.egress.host_callers)
    callers["runner"] = callers["runner"].model_copy(update={"categories": ["github_read", "web_public"]})
    with pytest.raises(render.RenderError, match="runner"):
        render.render_squid(_with_egress(bundle, host_callers=callers), "0" * 64, [NS], TEMPLATE)


def test_renderer_refuses_a_hand_built_context_giving_fetch_web_public() -> None:
    ctx = render.squid_context(_bundle(), "0" * 64, [NS], mode="open")
    ctx["rules"] = [*ctx["rules"], {"port": "sandbox", "src": "pool_fetch", "dst": "all"}]
    with pytest.raises(render.RenderError, match="fetch/validate"):
        render.render_context(ctx, TEMPLATE)
    ctx = render.squid_context(_bundle(), "0" * 64, [NS], mode="allowlist")
    ctx["rules"] = [*ctx["rules"], {"port": "hermes", "src": "src_host", "dst": "all"}]
    with pytest.raises(render.RenderError, match="allowlist"):
        render.render_context(ctx, TEMPLATE)


def test_renderer_refuses_mode_dependent_invariants() -> None:
    """A mode grant that changes a pool-mate list (agent-llm vs catalog) or drops an always-denied
    entry fails every render, whichever mode is asked for."""
    bundle = _bundle()
    modes = {k: dict(v) for k, v in bundle.egress.modes.items()}
    modes["research"] = {**modes["research"], "agent-llm": ["web_public"]}  # catalog not granted
    with pytest.raises(render.RenderError, match="share pool"):
        render.render_squid(_with_egress(bundle, modes=modes), "0" * 64, [NS], TEMPLATE, mode="allowlist")
    denied = bundle.egress.always_denied.model_copy(
        update={"domains": [d for d in bundle.egress.always_denied.domains if d != "telegra.ph"]}
    )
    with pytest.raises(render.RenderError, match=r"telegra\.ph"):
        render.render_squid(_with_egress(bundle, always_denied=denied), "0" * 64, [NS], TEMPLATE)
    cidrs = bundle.egress.always_denied.model_copy(
        update={"cidrs": [c for c in bundle.egress.always_denied.cidrs if c != "169.254.0.0/16"]}
    )
    with pytest.raises(render.RenderError, match=r"169\.254"):
        render.render_squid(_with_egress(bundle, always_denied=cidrs), "0" * 64, [NS], TEMPLATE)


def test_check_mode_invariants_catches_a_grant_to_the_code_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt and braces: if mode_lists ever leaked a grant into fetch, every render fails."""
    real = render.mode_lists

    def leaky(egress: EgressConfig, mode: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        profiles, callers = real(egress, mode)  # type: ignore[arg-type]
        if mode == "open":
            profiles = {**profiles, "fetch": [*profiles["fetch"], "web_public"]}
            profiles["validate"] = profiles["fetch"]
        return profiles, callers

    monkeypatch.setattr(render, "mode_lists", leaky)
    with pytest.raises(render.RenderError):
        render.render_squid(_bundle(), "0" * 64, [NS], TEMPLATE, mode="allowlist")


# ------------------------------------------------- sandbox_open and pool_strict (design §12.2)
# Design §12.2 table: which (port, source) gets web_public (``all``) in which mode.
WEB_MATRIX = {
    "allowlist": set(),
    "research": {("sandbox", "pool_research"), ("hermes", "src_host")},
    "open": {("sandbox", "pool_research"), ("hermes", "src_host"), ("sandbox", "pool_llm")},
    "sandbox_open": {("sandbox", "pool_research"), ("hermes", "src_host"), ("sandbox", "pool_llm"),
                     ("sandbox", "pool_fetch")},
}  # fmt: skip


@pytest.mark.parametrize("mode", MODES)
def test_mode_pool_matrix_rule_by_rule(mode: str) -> None:
    bundle = _bundle()
    ctx = render.squid_context(bundle, "0" * 64, [NS], mode=mode)  # type: ignore[arg-type]
    assert {(r["port"], r["src"]) for r in ctx["rules"] if r["dst"] == "all"} == WEB_MATRIX[mode]
    base = bundle.egress.sandbox_profiles
    by_src: dict[str, list[str]] = {}
    for r in ctx["rules"]:
        by_src.setdefault(r["src"] if r["port"] == "sandbox" else r["port"], []).append(r["dst"])
    cats = [f"cat_{c}" for c in base["fetch"] if f"cat_{c}" in {r["dst"] for r in ctx["rules"]}]
    assert by_src["pool_strict"] == cats  # the fetch base list, never a grant
    assert by_src["pool_fetch"] == cats + (["all"] if mode == "sandbox_open" else [])
    assert by_src["runner"] == ["cat_github_read"] and by_src["gatekeeper"] == ["cat_gatekeeper"]
    assert "all" not in by_src["pool_strict"]


def test_pool_strict_is_identical_in_all_four_goldens() -> None:
    texts = {m: (GOLDEN / f"squid.{m}.conf").read_text(encoding="utf-8") for m in MODES}
    strict = {
        m: [ln for ln in t.splitlines() if "pool_strict" in ln and not ln.startswith("#")] for m, t in texts.items()
    }
    assert strict["allowlist"] == strict["research"] == strict["open"] == strict["sandbox_open"]
    lines = strict["sandbox_open"]
    assert lines[0] == "acl pool_strict src 10.213.192.0/19"
    allows = lines[1:]
    assert allows and all(ln.startswith("http_access allow port_sandbox pool_strict cat_") for ln in allows)
    assert all(ln.endswith(" !denied_dst") for ln in allows)
    fetch = [ln.replace("pool_fetch", "pool_strict") for ln in texts["allowlist"].splitlines()
             if ln.startswith("http_access allow port_sandbox pool_fetch ")]  # fmt: skip
    assert allows == fetch


@pytest.mark.parametrize("mode", MODES)
def test_no_rendered_pool_overlaps_another(mode: str) -> None:
    ctx = render.squid_context(_bundle(), "0" * 64, [NS], mode=mode)  # type: ignore[arg-type]
    nets = [ipaddress.ip_network(p["cidr"]) for p in ctx["pools"]]
    assert [p["name"] for p in ctx["pools"]] == ["fetch", "research", "llm", "strict"]
    assert str(nets[-1]) == "10.213.192.0/19"
    for i, a in enumerate(nets):
        assert a.subnet_of(ipaddress.ip_network(render.TASK_NET))
        for b in nets[i + 1 :]:
            assert not a.overlaps(b), (a, b)
    assert not nets[-1].overlaps(ipaddress.ip_network("10.213.224.0/19"))  # the Phase-2 stack range


@pytest.mark.parametrize("mode", MODES)
def test_always_denied_precede_every_web_allow_and_floors_hold(mode: str) -> None:
    text = render.render_squid(_bundle(), "0" * 64, [NS], TEMPLATE, mode=mode)  # type: ignore[arg-type]
    lines = text.splitlines()
    denies = [lines.index(f"http_access deny {a}") for a in ("denied_domains", "ip_literal")]
    for i, line in enumerate(lines):
        if line.startswith("http_access allow ") and " all " in line:
            assert i > max(denies) and line.endswith(" !denied_dst"), line
    acls = _acls(text)
    assert set(render.REQUIRED_DENIED_DOMAINS) <= set(acls["denied_domains"][2:])
    denied = {str(ipaddress.ip_network(c)) for c in acls["denied_dst"][1:]}
    assert {str(ipaddress.ip_network(c)) for c in render.REQUIRED_DENIED_CIDRS} <= denied


@pytest.mark.parametrize("mode", MODES)
def test_log_format_carries_bytes_and_time(mode: str) -> None:
    text = render.render_squid(_bundle(), "0" * 64, [NS], TEMPLATE, mode=mode)  # type: ignore[arg-type]
    assert "logformat ossbot %ts.%03tu %>a %>lp %Ss/%03>Hs %rm %ru %<a %>st %<st %tr\n" in text
    assert "access_log stdio:/var/log/squid/access.log logformat=ossbot rotate=0\n" in text


def _grant_code_web(bundle: render.ConfigBundle, mode: str) -> render.ConfigBundle:
    """Bypass the schema validator (model_copy) to hand the renderer a fetch/validate grant."""
    modes = {k: dict(v) for k, v in bundle.egress.modes.items()}
    modes[mode] = {**modes[mode], "fetch": ["web_public"], "validate": ["web_public"]}  # type: ignore[index]
    return _with_egress(bundle, modes=modes)


@pytest.mark.parametrize("mode", ["allowlist", "research", "open"])
def test_check_mode_invariants_fails_when_fetch_gets_web_public_outside_sandbox_open(mode: str) -> None:
    bundle = _grant_code_web(_bundle(), mode)
    with pytest.raises(render.RenderError, match="only in egress mode sandbox_open"):
        render.check_mode_invariants(bundle, [NS])
    with pytest.raises(render.RenderError, match="only in egress mode sandbox_open"):
        render.render_squid(bundle, "0" * 64, [NS], TEMPLATE, mode="sandbox_open")


def test_renderer_refuses_web_public_for_the_strict_pool_and_llm_outside_open() -> None:
    ctx = render.squid_context(_bundle(), "0" * 64, [NS], mode="sandbox_open")
    ctx["rules"] = [*ctx["rules"], {"port": "sandbox", "src": "pool_strict", "dst": "all"}]
    with pytest.raises(render.RenderError, match="pool_strict"):
        render.render_context(ctx, TEMPLATE)
    ctx = render.squid_context(_bundle(), "0" * 64, [NS], mode="research")
    ctx["rules"] = [*ctx["rules"], {"port": "sandbox", "src": "pool_llm", "dst": "all"}]
    with pytest.raises(render.RenderError, match="agent-llm/catalog"):
        render.render_context(ctx, TEMPLATE)
    ctx = render.squid_context(_bundle(), "0" * 64, [NS], mode="open")
    ctx["rules"] = [*ctx["rules"], {"port": "gatekeeper", "src": "src_host", "dst": "all"}]
    with pytest.raises(render.RenderError, match="gatekeeper"):
        render.render_context(ctx, TEMPLATE)


def test_strict_pool_ignores_the_mode_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    """pool_strict comes from egress.sandbox_profiles['fetch'] directly: a mode_lists leak can't reach it."""
    real = render.mode_lists

    def leaky(egress: EgressConfig, mode: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        profiles, callers = real(egress, mode)  # type: ignore[arg-type]
        widened = [*profiles["fetch"], "kiro_core"]
        return {**profiles, "fetch": widened, "validate": widened}, callers

    monkeypatch.setattr(render, "mode_lists", leaky)
    ctx = render.squid_context(_bundle(), "0" * 64, [NS], mode="sandbox_open")
    assert "cat_kiro_core" not in {r["dst"] for r in ctx["rules"] if r["src"] == "pool_strict"}
    assert "cat_kiro_core" in {r["dst"] for r in ctx["rules"] if r["src"] == "pool_fetch"}


# ------------------------------------------------------------------- live drift (--check)
def _snapshot_env(tmp_path: Path, mode: str | None) -> tuple[Path, Path, Path, Path]:
    files = {n: (ROOT / "config" / n).read_bytes() for n in ("policy.yaml", "egress.yaml", "targets.yaml")}
    files["profile.yaml"] = (ROOT / "tests" / "fixtures" / "profile.test.yaml").read_bytes()
    root = tmp_path / "snap"
    digest = write_snapshot(root, files)
    control = tmp_path / "control"
    control.mkdir()
    if mode is not None:
        write_flag(control / "egress-mode", {"mode": mode, "actor": "test", "ts": "x"})
    resolv = tmp_path / "resolv.conf"
    resolv.write_text(f"nameserver {NS}\n")
    template = tmp_path / "squid.conf.j2"
    template.write_text(TEMPLATE, encoding="utf-8")
    conf = tmp_path / "squid.conf"
    conf.write_text(render.render_squid(parse_bundle(files), digest, [NS], TEMPLATE, mode=mode or "open"))  # type: ignore[arg-type]
    return conf, root, template, resolv


def _drift(conf: Path, root: Path, template: Path, resolv: Path, control: Path, now: float | None = None) -> list[str]:
    return render.squid_drift(conf, root, template, resolv, control, now)


def test_squid_drift_none_when_live_matches_the_mode(tmp_path: Path) -> None:
    conf, root, template, resolv = _snapshot_env(tmp_path, "research")
    assert _drift(conf, root, template, resolv, tmp_path / "control") == []
    assert _drift(tmp_path / "absent.conf", root, template, resolv, tmp_path / "control") == []  # not rendered yet


def test_squid_drift_after_a_mode_switch_without_apply(tmp_path: Path) -> None:
    conf, root, template, resolv = _snapshot_env(tmp_path, "research")
    control = tmp_path / "control"
    old = time.time() - 600
    os.utime(conf, (old, old))
    write_flag(control / "egress-mode", {"mode": "allowlist", "actor": "test", "ts": "x"})
    flag_mtime = (control / "egress-mode").stat().st_mtime
    assert _drift(conf, root, template, resolv, control, now=flag_mtime + 5) == []  # apply pending (grace)
    [problem] = _drift(conf, root, template, resolv, control, now=flag_mtime + render.MODE_APPLY_GRACE_S + 1)
    assert problem.startswith("firewall_drift:squid_mode ") and "allowlist" in problem


def test_squid_drift_open_login_window_needs_the_login_flag(tmp_path: Path) -> None:
    """F005-R2-4: the window state comes from `ossctl hermes login`'s flag, not from the conf under test."""
    conf, root, template, resolv = _snapshot_env(tmp_path, "research")
    control, flag = tmp_path / "control", tmp_path / "login-window"
    files = {n: (ROOT / "config" / n).read_bytes() for n in ("policy.yaml", "egress.yaml", "targets.yaml")}
    files["profile.yaml"] = (ROOT / "tests" / "fixtures" / "profile.test.yaml").read_bytes()
    digest = write_snapshot(root, files)  # same content, same digest as _snapshot_env's
    conf.write_text(
        render.render_squid(parse_bundle(files), digest, [NS], TEMPLATE, hermes_login=True, mode="research")
    )
    [problem] = render.squid_drift(conf, root, template, resolv, control, login_window_flag=flag)
    assert problem.startswith("firewall_drift:squid_mode ")  # left open, no login running
    flag.touch()
    assert render.squid_drift(conf, root, template, resolv, control, login_window_flag=flag) == []  # login running
    conf.write_text(render.render_squid(parse_bundle(files), digest, [NS], TEMPLATE, mode="research"))
    assert render.squid_drift(conf, root, template, resolv, control, login_window_flag=flag) == []  # closing


def test_squid_drift_unparseable_flag_means_allowlist(tmp_path: Path) -> None:
    conf, root, template, resolv = _snapshot_env(tmp_path, "open")
    control = tmp_path / "control"
    old = time.time() - 600
    (control / "egress-mode").write_text("{not json")
    os.utime(control / "egress-mode", (old, old))
    [problem] = _drift(conf, root, template, resolv, control)
    assert "egress mode allowlist" in problem


def test_squid_drift_detects_a_hand_edit_and_a_missing_snapshot(tmp_path: Path) -> None:
    conf, root, template, resolv = _snapshot_env(tmp_path, None)
    control = tmp_path / "control"
    text = conf.read_text()
    conf.write_text(text.replace("http_access deny ip_literal\n", ""))
    [problem] = _drift(conf, root, template, resolv, control)
    assert problem.startswith("firewall_drift:squid_mode")
    conf.write_text(text.replace(text.split("# Config snapshot: ")[1][:64], "f" * 64))
    [problem] = _drift(conf, root, template, resolv, control)
    assert "cannot read the snapshot" in problem
    conf.write_text("http_access allow all\n")
    [problem] = _drift(conf, root, template, resolv, control)
    assert "names no config snapshot" in problem


def test_squid_drift_tolerates_a_snapshot_the_new_schema_rejects(tmp_path: Path) -> None:
    """A deploy that changes the schema must not deadlock the gatekeeper start (its ExecStartPre
    runs --check before it can write the new snapshot); a missing snapshot still fails."""
    conf, root, template, resolv = _snapshot_env(tmp_path, None)
    files = {p.name: p.read_bytes() for p in (root / "current").iterdir()}
    files["targets.yaml"] = files["targets.yaml"].replace(b"  size_class: null", b"  max_changed_lines: 400")
    old = write_snapshot(root, files)
    text = conf.read_text()
    conf.write_text(text.replace(text.split("# Config snapshot: ")[1][:64], old))
    assert (root / "current").readlink().name == old  # write_snapshot repointed current to it
    assert _drift(conf, root, template, resolv, tmp_path / "control") == []
    assert render.squid_snapshot_rejected(conf, root) == old  # doctor WARNs meanwhile


def test_squid_drift_bounds_the_rejected_snapshot_window(tmp_path: Path) -> None:
    """Review F005-R1-5: once a newer snapshot is current and older than the grace, a conf still
    naming the rejected one is drift (the proxy missed its re-render)."""
    conf, root, template, resolv = _snapshot_env(tmp_path, None)
    good = (root / "current").readlink().name
    files = {p.name: p.read_bytes() for p in (root / "current").iterdir()}
    files["targets.yaml"] = files["targets.yaml"].replace(b"  size_class: null", b"  max_changed_lines: 400")
    old = write_snapshot(root, files)
    (root / "current").unlink()  # the gatekeeper wrote a newer, valid snapshot
    os.symlink(good, root / "current")
    text = conf.read_text()
    conf.write_text(text.replace(text.split("# Config snapshot: ")[1][:64], old))
    young = (root / "current").lstat().st_mtime
    assert _drift(conf, root, template, resolv, tmp_path / "control", now=young + 5) == []
    [problem] = _drift(conf, root, template, resolv, tmp_path / "control", now=young + render.MODE_APPLY_GRACE_S + 1)
    assert problem.startswith("firewall_drift:squid_mode") and "rejects" in problem
    assert render.squid_snapshot_rejected(conf, root) == old
    conf.write_text(text)
    assert render.squid_snapshot_rejected(conf, root) is None


def test_squid_listeners_and_classes() -> None:
    text = render.render_squid(_bundle(), "0" * 64, [NS], TEMPLATE, mode="research")
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
