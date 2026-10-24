"""build-env FEAT-001: the firewall render foundations (``Chain.table``, ``Chain.parent = None``, the
arity-aware ``--check`` normaliser, the shlex listing parser; design §4.4) and the F1c MSS clamp
(``OSSBOT-MSS-FWD/-OUT/-IN`` in the mangle table, design §7), checked against a listing recorded from
the real iptables 1.8.10 in a scratch netns (``tests/system/capture_firewall_fixture.sh``, L-6)."""

from __future__ import annotations

import inspect
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ossbot.common.control_flags import EGRESS_MODES
from ossbot.ctl import doctor, render

ROOT = Path(__file__).resolve().parents[3]
GOLDEN = Path(__file__).parent / "golden" / "iptables.rules"
FIXTURE = ROOT / "tests" / "fixtures" / "firewall" / "iptables-S-1.8.10-nft.txt"
NS = "10.255.255.254"
MSS = {"OSSBOT-MSS-FWD": ("FORWARD", ()), "OSSBOT-MSS-OUT": ("OUTPUT", ("!", "-o", "lo")),
       "OSSBOT-MSS-IN": ("INPUT", ("!", "-i", "lo"))}  # fmt: skip


def _norm(rule: str) -> frozenset[tuple[str, str]]:
    return render.normalize_rule(rule.split())


# ------------------------------------------------------------------------------ MSS chains
def test_firewall_plan_has_no_mode_input() -> None:
    """The plan never depends on the egress mode, so the clamp is live in every mode (F1c: the
    allowlist path too, squid originates every upstream connection)."""
    assert list(inspect.signature(render.firewall_plan).parameters) == ["ns", "uids", "host_cidrs", "public"]


@pytest.mark.parametrize("mode", EGRESS_MODES)
def test_mss_chains_are_in_part_host_in_every_mode(mode: str) -> None:
    del mode  # the plan has no mode input (above); one case per mode documents the requirement
    plan = render.firewall_plan(NS)
    mss = [c for c in plan["host"] if c.name.startswith("OSSBOT-MSS-")]
    assert [(c.family, c.name, c.parent, c.table) for c in mss] == [
        ("iptables", name, parent, "mangle") for name, (parent, _) in MSS.items()
    ]
    assert not [c for c in plan["docker"] if c.table != "filter"]
    for c in mss:
        _, extra = MSS[c.name]
        assert c.rules == (
            ("-p", "tcp", "-m", "tcp", "--tcp-flags", "SYN,RST", "SYN", *extra, "-j", "TCPMSS", "--set-mss", "1300"),
        )
    assert [c.table for c in plan["host"] if not c.name.startswith("OSSBOT-MSS-")] == ["filter"] * 5


def test_render_names_the_table_only_for_mangle_chains() -> None:
    text = render.render_script(render.select(render.firewall_plan(NS), "all"))
    mangle = [line for line in text.splitlines() if " -t " in line]
    assert len(mangle) == 9 and all(line.startswith("iptables -t mangle ") for line in mangle)
    assert all("OSSBOT-MSS-" in line for line in mangle)
    assert "iptables -t mangle -C FORWARD -j OSSBOT-MSS-FWD 2>/dev/null || " \
        "iptables -t mangle -I FORWARD 1 -j OSSBOT-MSS-FWD" in mangle  # fmt: skip
    assert not [line for line in text.splitlines() if "OSSBOT-MSS-" in line and " -t mangle " not in line]


def test_filter_lines_stay_byte_identical_to_the_golden() -> None:
    """The golden only gained the mangle lines; every filter line keeps its form and order."""
    plan = render.firewall_plan(NS)
    filter_only = [c for c in render.select(plan, "all") if c.table == "filter"]
    golden = GOLDEN.read_text(encoding="utf-8").splitlines()
    assert render.render_script(filter_only).splitlines() == [
        line for line in golden if not line.startswith("iptables -t mangle ")
    ]


def test_parentless_chain_has_no_jump_anywhere() -> None:
    c = render.Chain("iptables", "OSSBOT-X", None, (("-j", "DROP"),))
    assert render.render_script([c]) == (
        "iptables -N OSSBOT-X 2>/dev/null || iptables -F OSSBOT-X\niptables -A OSSBOT-X -j DROP\n"
    )
    listing = "-N OSSBOT-X\n-A OSSBOT-X -j DROP\n"
    assert render.check_chain(c, listing, "") == []
    assert render.check_chain(c, "-N OSSBOT-X\n", "")  # its rules are still compared


# ----------------------------------------------------------------------- apply/check argv
class _Recorder:
    def __init__(self, listings: dict[tuple[str, str, str], str] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.listings = listings or {}

    def __call__(self, family: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        self.calls.append((family, *args))
        if "-S" in args:
            key = (family, args[args.index("-t") + 1], args[-1])
            text = self.listings.get(key)
            return subprocess.CompletedProcess([family, *args], 0 if text is not None else 1, text or "", "")
        rc = 1 if args[2:3] == ("-D",) else 0  # no old jump to delete
        return subprocess.CompletedProcess([family, *args], rc, "", "")


def test_apply_and_check_pass_the_table_to_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    chains = render.select(render.firewall_plan(NS), "all")
    rec = _Recorder()
    monkeypatch.setattr(render, "_ipt", rec)
    render.apply(chains)
    assert rec.calls and all(call[1] == "-t" for call in rec.calls)
    tables = {(call[0], call[2], call[4]) for call in rec.calls}
    for c in chains:
        assert (c.family, c.table, c.name) in tables
    assert ("iptables", "-t", "mangle", "-I", "INPUT", "1", "-j", "OSSBOT-MSS-IN") in rec.calls
    assert ("iptables", "-t", "filter", "-I", "INPUT", "1", "-j", "OSSBOT-IN") in rec.calls

    rec = _Recorder()
    monkeypatch.setattr(render, "_ipt", rec)
    problems = render.check(chains, NS)
    assert all(call[1] == "-t" and call[3] == "-S" for call in rec.calls)
    assert ("iptables", "-t", "mangle", "-S", "OSSBOT-MSS-OUT") in rec.calls
    assert ("iptables", "-t", "mangle", "-S", "OUTPUT") in rec.calls
    assert "iptables -t mangle OSSBOT-MSS-FWD: chain missing" in problems
    assert "iptables OSSBOT-IN: chain missing" in problems  # filter messages keep their form


def test_apply_skips_the_jump_of_a_parentless_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(render, "_ipt", rec)
    render.apply([render.Chain("iptables", "OSSBOT-X", None, (("-j", "DROP"),))])
    assert [call[3] for call in rec.calls] == ["-N", "-A"]
    rec = _Recorder({("iptables", "filter", "OSSBOT-X"): "-N OSSBOT-X\n-A OSSBOT-X -j DROP\n"})
    monkeypatch.setattr(render, "_ipt", rec)
    assert render.check([render.Chain("iptables", "OSSBOT-X", None, (("-j", "DROP"),))], NS) == []
    assert rec.calls == [("iptables", "-t", "filter", "-S", "OSSBOT-X")]


def test_check_reports_mangle_drift_with_the_table() -> None:
    [c] = [c for c in render.firewall_plan(NS)["host"] if c.name == "OSSBOT-MSS-FWD"]
    good = "-N OSSBOT-MSS-FWD\n-A OSSBOT-MSS-FWD -p tcp -m tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1300\n"
    parent = "-P FORWARD ACCEPT\n-A FORWARD -j OSSBOT-MSS-FWD\n"
    assert render.check_chain(c, good, parent) == []
    raised = good.replace("1300", "1460")
    want = ["iptables -t mangle OSSBOT-MSS-FWD: rules differ from the rendered set"]
    assert render.check_chain(c, raised, parent) == want
    assert render.check_chain(c, good, "-A FORWARD -j OTHER\n-A FORWARD -j OSSBOT-MSS-FWD\n") == [
        "iptables -t mangle FORWARD: jump to OSSBOT-MSS-FWD is not at position 1",
    ]


# --------------------------------------------------------------------------- normaliser
def test_tcp_flags_take_two_values_compared_as_sorted_sets() -> None:
    assert _norm("-p tcp --tcp-flags SYN,RST SYN -j X") == _norm("-p tcp -m tcp --tcp-flags RST,SYN SYN -j X")
    assert ("--tcp-flags", "RST,SYN SYN") in _norm("-p tcp --tcp-flags SYN,RST SYN -j X")
    assert _norm("-p tcp --tcp-flags FIN,SYN,RST,ACK SYN") == _norm("-p tcp --tcp-flags ACK,FIN,RST,SYN SYN")
    assert _norm("-p tcp --tcp-flags SYN,RST SYN") != _norm("-p tcp --tcp-flags SYN,RST RST")
    assert ("?", "--tcp-flags") in _norm("-p tcp --tcp-flags SYN,RST")  # one value short: drift


@pytest.mark.parametrize("opt", sorted(set(render._ARITY) - {"--tcp-flags"}))
def test_single_value_options(opt: str) -> None:
    assert (opt, "v") in _norm(f"{opt} v") or opt in {"-s", "-d", "-m"}
    assert ("?", opt) in _norm(opt)  # missing value: drift


def test_printed_defaults_are_dropped_on_both_sides() -> None:
    log = "-j LOG --log-prefix OSSBOT-FLOOR-"
    assert _norm(f"{log} --log-level 4") == _norm(log) == _norm(f"{log} --log-level warning")
    assert _norm(f"{log} --log-level 6") != _norm(log)
    assert _norm("-j REJECT --reject-with icmp-port-unreachable") == _norm("-j REJECT")
    assert _norm("-j REJECT --reject-with icmp6-port-unreachable") == _norm("-j REJECT")
    assert _norm("-j REJECT --reject-with tcp-reset") != _norm("-j REJECT")


def test_hashlimit_tuning_is_compared_only_when_rendered() -> None:
    base = ("-s 10.213.0.0/16 -m hashlimit --hashlimit-upto 20/sec --hashlimit-burst 40 --hashlimit-mode srcip "
            "--hashlimit-name ossbot_floor_in -j LOG --log-prefix OSSBOT-FLOOR-")  # fmt: skip
    chain = render.Chain("iptables", "OSSBOT-T", None, (tuple(base.split()),))
    tuned = f"-N OSSBOT-T\n-A OSSBOT-T {base} --hashlimit-htable-expire 10000\n"
    assert render.check_chain(chain, tuned, "") == []  # tuning the rendered rule leaves unset
    rendered_tuned = render.Chain("iptables", "OSSBOT-T", None, ((*base.split(), "--hashlimit-htable-max", "64"),))
    assert render.check_chain(rendered_tuned, f"-N OSSBOT-T\n-A OSSBOT-T {base} --hashlimit-htable-max 64\n", "") == []
    assert render.check_chain(rendered_tuned, f"-N OSSBOT-T\n-A OSSBOT-T {base} --hashlimit-htable-max 128\n", "")
    assert render.check_chain(rendered_tuned, f"-N OSSBOT-T\n-A OSSBOT-T {base}\n", "")
    # what is dropped or logged is never tuning
    assert render.check_chain(chain, f"-N OSSBOT-T\n-A OSSBOT-T {base.replace('20/sec', '2000/sec')}\n", "")


def test_unknown_options_stay_drift() -> None:
    assert ("?", "--comment") in _norm("-m comment --comment x -j DROP")
    assert ("?", "--syn") in _norm("-p tcp --syn -j DROP")  # never rendered: iptables -S prints --tcp-flags


def test_listing_parser_uses_shlex() -> None:
    listing = '-N OSSBOT-T\n-A OSSBOT-T -j LOG --log-prefix "OSSBOT FLOOR "\n'
    assert render.parse_listing(listing, "OSSBOT-T") == [frozenset({("-j", "LOG"), ("--log-prefix", "OSSBOT FLOOR ")})]
    with pytest.raises(ValueError, match="mangle OSSBOT-T: unparseable"):
        render.parse_listing('-A OSSBOT-T -j LOG --log-prefix "open\n', "OSSBOT-T", "mangle")
    chain = render.Chain("iptables", "OSSBOT-T", "INPUT", (("-j", "DROP"),), "mangle")
    [problem] = render.check_chain(chain, '-A OSSBOT-T -j "DROP\n', "-A INPUT -j OSSBOT-T\n")
    assert problem.startswith("iptables mangle OSSBOT-T: unparseable listing line")


# ------------------------------------------------------------------- recorded live listing
@dataclass
class _Fixture:
    ns: str = ""
    uids: dict[str, str] = field(default_factory=dict)
    hosts: list[str] = field(default_factory=list)
    public: list[str] = field(default_factory=list)
    sections: dict[tuple[str, str], str] = field(default_factory=dict)

    def plan(self) -> dict[str, list[render.Chain]]:
        return render.firewall_plan(self.ns, self.uids, self.hosts, self.public)


def _fixture() -> _Fixture:
    fx = _Fixture()
    key: tuple[str, str] | None = None
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            family, table = line[3:].split()
            key = (family, table)
            fx.sections[key] = ""
        elif line.startswith("# ns "):
            fx.ns = line.split()[2]
        elif line.startswith("# uid "):
            _, _, user, uid = line.split()
            fx.uids[user] = uid
        elif line.startswith("# host "):
            fx.hosts.append(line.split()[2])
        elif line.startswith("# public "):
            fx.public.append(line.split()[2])
        elif key is not None:
            fx.sections[key] += line + "\n"
    return fx


def test_check_matches_recorded_listing() -> None:
    """L-6: check_chain reports nothing for every chain of the plan against what iptables 1.8.10
    (nf_tables) printed after applying the rendered script in a scratch netns (with a fixed TEST-NET
    public floor and host /32, so OSSBOT-PUBFLOOR and the host rules of the proxy floor are recorded)."""
    fx = _fixture()
    assert fx.ns and set(fx.uids) == set(render.SERVICE_USERS)
    assert fx.hosts == ["192.0.2.10/32"] and fx.public == ["203.0.113.7/32"]
    chains = render.select(fx.plan(), "all")
    assert {(c.family, c.table) for c in chains} <= set(fx.sections)
    for c in chains:
        listing = fx.sections[(c.family, c.table)]
        assert f"-N {c.name}\n" in listing, (c.family, c.table, c.name)
        assert render.check_chain(c, listing, listing) == [], (c.family, c.table, c.name)
    assert not render.resolver_drift(fx.sections[("iptables", "filter")], fx.ns)
    [pub] = [c for c in chains if c.name == "OSSBOT-PUBFLOOR"]
    assert len(pub.rules) == 6 and "-d 203.0.113.7/32" in fx.sections[("iptables", "filter")]


def test_recorded_listing_shows_drift_once_changed() -> None:
    fx = _fixture()
    ns, uids, sections = fx.ns, fx.uids, fx.sections
    [c] = [c for c in render.firewall_plan(ns, uids)["host"] if c.name == "OSSBOT-MSS-OUT"]
    listing = sections[("iptables", "mangle")].replace("--set-mss 1300", "--set-mss 1400")
    assert render.check_chain(c, listing, listing)


# ------------------------------------------------------------- FEAT-002 kernel floors (§4.4, §4.7)
PX = ("-m", "conntrack", "--ctstate", "NEW", "-m", "hashlimit", "--hashlimit-upto", "20/sec", "--hashlimit-burst",
      "40", "--hashlimit-mode", "dstip", "--hashlimit-name", "ossbot_floor_px", "-j", "LOG", "--log-prefix",
      "OSSBOT-PFLOOR-")  # fmt: skip
TX = ("-m", "conntrack", "--ctstate", "NEW", "-m", "hashlimit", "--hashlimit-upto", "20/sec", "--hashlimit-burst",
      "40", "--hashlimit-mode", "srcip,dstip", "--hashlimit-name", "ossbot_floor_tx", "-j", "LOG", "--log-prefix",
      "OSSBOT-TFLOOR-")  # fmt: skip
DROP_NEW = ("-m", "conntrack", "--ctstate", "NEW", "-j", "DROP")


def _block(d: str) -> list[tuple[str, ...]]:
    return [
        ("-s", "10.212.0.2/32", "-d", d, *PX), ("-s", "10.212.0.2/32", "-d", d, *DROP_NEW),
        ("-s", "10.212.0.0/24", "-d", d, *TX), ("-s", "10.212.0.0/24", "-d", d, *DROP_NEW),
        ("-s", "10.211.0.0/24", "-d", d, *DROP_NEW), ("-s", "10.214.0.0/16", "-d", d, *DROP_NEW),
    ]  # fmt: skip


def test_fwd_starts_with_the_public_floor_then_the_proxy_floor_per_range_and_host() -> None:
    """AC 20: OSSBOT-FWD = -j OSSBOT-PUBFLOOR, then per IPv4 floor range and live host /32 the source split
    (PFLOOR LOG + DROP for the live proxy, TFLOOR LOG + DROP for the rest of ossbot-egress, unlogged drops for
    builds and root-created networks), then the unchanged 10.213 rule."""
    plan = render.firewall_plan(NS, host_cidrs=["192.0.2.10/32"])
    [pub, fwd] = plan["docker"]
    assert (pub.name, pub.parent, pub.rules) == ("OSSBOT-PUBFLOOR", None, ())
    want = [("-j", "OSSBOT-PUBFLOOR")]
    for d in (*render.IPV4_FLOOR_CIDRS, "192.0.2.10/32"):
        want += _block(d)
    want.append(("-s", "10.213.0.0/16", "!", "-d", "10.213.0.0/16", "-j", "DROP"))
    assert list(fwd.rules) == want
    assert {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "198.18.0.0/15",
            "192.0.0.0/24", "224.0.0.0/4", "240.0.0.0/4"} <= set(render.IPV4_FLOOR_CIDRS)  # fmt: skip
    # nothing drops squid's DNS: the resolver is local (INPUT), never a FORWARD destination of these rules
    assert not [r for r in fwd.rules if "10.255.255.254/32" in r or "--dport" in r]


def test_in_logs_sandbox_packets_and_splits_the_proxy_input_drop_by_source() -> None:
    """§4.4 (a) and (c): the resolver accepts first, the two OSSBOT-FLOOR- LOG rules before the 10.213 DROP,
    then PFLOOR/TFLOOR LOG + DROP pairs that only split the existing final 10.212.0.0/24 NEW DROP."""
    rules = render.firewall_plan(NS)["host"][0].rules
    hl_in = ("-m", "hashlimit", "--hashlimit-upto", "20/sec", "--hashlimit-burst", "40", "--hashlimit-mode", "srcip",
             "--hashlimit-name", "ossbot_floor_in", "-j", "LOG", "--log-prefix", "OSSBOT-FLOOR-")  # fmt: skip
    assert list(rules) == [
        ("-s", "10.212.0.2/32", "-d", f"{NS}/32", "-p", "udp", "--dport", "53", "-j", "ACCEPT"),
        ("-s", "10.212.0.2/32", "-d", f"{NS}/32", "-p", "tcp", "--dport", "53", "-j", "ACCEPT"),
        ("-s", "10.213.0.0/16", "-p", "tcp", "-m", "tcp", "--tcp-flags", "FIN,SYN,RST,ACK", "SYN", "-m", "pkttype",
         "--pkt-type", "unicast", *hl_in),
        ("-s", "10.213.0.0/16", "-p", "udp", "-m", "udp", "-m", "pkttype", "--pkt-type", "unicast", *hl_in),
        ("-s", "10.213.0.0/16", "-j", "DROP"),
        ("-s", "10.212.0.2/32", *PX[:4], *PX[4:13], "ossbot_floor_pin", *PX[14:]),
        ("-s", "10.212.0.2/32", *DROP_NEW),
        ("-s", "10.212.0.0/24", *TX[:4], *TX[4:13], "ossbot_floor_tin", *TX[14:]),
        ("-s", "10.212.0.0/24", *DROP_NEW),
    ]  # fmt: skip


def test_pubfloor_from_the_public_file_skips_ipv6() -> None:
    plan = render.firewall_plan(NS, public=["203.0.113.7/32", "2001:db8::7/128", "198.51.100.9/32"])
    [pub] = [c for c in plan["docker"] if c.name == "OSSBOT-PUBFLOOR"]
    assert list(pub.rules) == _block("203.0.113.7/32") + _block("198.51.100.9/32")
    assert render.select(plan, "docker")[0].name == "OSSBOT-PUBFLOOR"  # created before the jump to it
    assert render.select(plan, "all")[-2].name == "OSSBOT-PUBFLOOR"


def test_log_prefixes_print_unquoted() -> None:
    """§4.4 item 2: only [A-Za-z0-9_-], which xtables_save_string prints unquoted (V-I1)."""
    plan = render.firewall_plan(NS, host_cidrs=["192.0.2.10/32"], public=["203.0.113.7/32"])
    prefixes = {r[r.index("--log-prefix") + 1] for c in render.select(plan, "all") for r in c.rules
                if "--log-prefix" in r}  # fmt: skip
    assert prefixes == {"OSSBOT-FLOOR-", "OSSBOT-PFLOOR-", "OSSBOT-TFLOOR-"}
    assert all(re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in prefixes)


def _listing_of(chain: render.Chain) -> str:
    return "\n".join([f"-N {chain.name}", *(f"-A {chain.name} {' '.join(r)}" for r in chain.rules)]) + "\n"


def test_check_reports_public_floor_and_host_floor_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    live = render.firewall_plan(NS, host_cidrs=["192.0.2.10/32"], public=["203.0.113.7/32"])
    listings = {("iptables", "filter", c.name): _listing_of(c) for c in live["docker"]}
    listings[("iptables", "filter", "DOCKER-USER")] = "-N DOCKER-USER\n-A DOCKER-USER -j OSSBOT-FWD\n"
    rec = _Recorder(listings)
    monkeypatch.setattr(render, "_ipt", rec)
    assert render.check(live["docker"], NS) == []
    moved = render.firewall_plan(NS, host_cidrs=["192.0.2.11/32"], public=["203.0.113.7/32"])
    problems = render.check(moved["docker"], NS)
    assert any(p.startswith("firewall_drift:host_floor_changed") for p in problems)
    assert not any("public_floor" in p for p in problems)
    new_pub = render.firewall_plan(NS, host_cidrs=["192.0.2.10/32"], public=["198.51.100.9/32"])
    problems = render.check(new_pub["docker"], NS)
    assert any(p.startswith("firewall_drift:public_floor") for p in problems)
    assert not any("host_floor_changed" in p for p in problems)


@pytest.mark.parametrize(("part", "locked"), [("docker", True), ("all", True), ("host", False)])
def test_apply_takes_the_floor_lock_exclusively_for_the_parts_touching_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, part: str, locked: bool
) -> None:
    """C-M1: --apply holds the root-only floor lock exclusively around reading public.txt and applying every
    part that touches OSSBOT-FWD/OSSBOT-PUBFLOOR, so a concurrent pubip update can't be flushed away."""
    resolv = tmp_path / "resolv.conf"
    resolv.write_text(f"nameserver {NS}\n")
    floor = tmp_path / "floor"
    floor.mkdir()
    (floor / "public.txt").write_text("240.0.0.1/32\n203.0.113.7/32\n")
    events: list[str] = []

    class Lock:
        def __init__(self, exclusive: bool, timeout_s: float, path: Path) -> None:
            events.append(f"lock {'ex' if exclusive else 'sh'} {path.name} {timeout_s:g}")

        def __enter__(self) -> None:
            events.append("enter")

        def __exit__(self, *exc: object) -> None:
            events.append("exit")

    applied: list[list[render.Chain]] = []
    monkeypatch.setattr(render, "floor_lock", Lock)
    monkeypatch.setattr(render, "resolve_uids", lambda: {u: u for u in render.SERVICE_USERS})
    monkeypatch.setattr(render, "host_floor_cidrs", lambda **kw: ())
    monkeypatch.setattr(render, "apply", lambda chains: (events.append("apply"), applied.append(list(chains))))
    args = ["firewall", "--apply", "--part", part, "--resolv-conf", str(resolv), "--floor-dir", str(floor)]
    assert render.main(args) == 0
    if locked:
        assert events == ["lock ex .lock 30", "enter", "apply", "exit"]
        [pub] = [c for c in applied[0] if c.name == "OSSBOT-PUBFLOOR"]
        assert pub.rules == tuple(_block("203.0.113.7/32"))  # read under the lock
    else:
        assert events == ["apply"]


def test_check_takes_the_floor_lock_shared_with_a_10_s_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resolv = tmp_path / "resolv.conf"
    resolv.write_text(f"nameserver {NS}\n")
    events: list[str] = []

    class Lock:
        def __init__(self, exclusive: bool, timeout_s: float, path: Path) -> None:
            events.append(f"lock {'ex' if exclusive else 'sh'} {timeout_s:g}")

        def __enter__(self) -> None:
            events.append("enter")

        def __exit__(self, *exc: object) -> None:
            events.append("exit")

    monkeypatch.setattr(render, "floor_lock", Lock)
    monkeypatch.setattr(render, "resolve_uids", lambda: {u: u for u in render.SERVICE_USERS})
    monkeypatch.setattr(render, "host_floor_cidrs", lambda **kw: ())
    monkeypatch.setattr(render, "check", lambda chains, ns: (events.append("check"), [])[1])
    monkeypatch.setattr(render, "resolved_ipc_problems", lambda: [])
    monkeypatch.setattr(render, "io_uring_problems", lambda: [])
    monkeypatch.setattr(render, "squid_drift", lambda **kw: (events.append("squid_drift"), [])[1])
    args = ["firewall", "--check", "--resolv-conf", str(resolv), "--floor-dir", str(tmp_path / "floor")]
    assert render.main(args) == 0
    assert events == ["lock sh 10", "enter", "check", "squid_drift", "exit"]


def test_a_busy_or_untrusted_lock_fails_the_check_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    from ossbot.common.floor_lock import FloorLockBusy, FloorLockUntrusted

    resolv = tmp_path / "resolv.conf"
    resolv.write_text(f"nameserver {NS}\n")
    monkeypatch.setattr(render, "resolve_uids", lambda: {u: u for u in render.SERVICE_USERS})
    for exc in (FloorLockBusy("held"), FloorLockUntrusted("mode 0644")):

        class Lock:
            def __init__(self, exclusive: bool, timeout_s: float, path: Path, _e: Exception = exc) -> None:
                self.e = _e

            def __enter__(self) -> None:
                raise self.e

            def __exit__(self, *a: object) -> None:
                return None

        monkeypatch.setattr(render, "floor_lock", Lock)
        assert render.main(["firewall", "--check", "--resolv-conf", str(resolv), "--floor-dir", str(tmp_path)]) == 2
        assert exc.code in capsys.readouterr().err


# ------------------------------------------------------------------------------- doctor
@pytest.mark.parametrize(("value", "status"), [("1\n", "PASS"), ("0\n", "WARN"), ("2\n", "WARN"), (None, "WARN")])
def test_doctor_warns_when_host_plpmtud_is_off(tmp_path: Path, value: str | None, status: str) -> None:
    sysctl = tmp_path / "tcp_mtu_probing"
    if value is not None:
        sysctl.write_text(value)
    result: Any = doctor.check_tcp_mtu_probing(sysctl)
    assert result.status == status and result.name == "tcp_mtu_probing"
    assert status == "PASS" or "expected 1" in result.detail
