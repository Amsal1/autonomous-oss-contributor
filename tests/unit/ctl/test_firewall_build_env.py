"""build-env FEAT-001: the firewall render foundations (``Chain.table``, ``Chain.parent = None``, the
arity-aware ``--check`` normaliser, the shlex listing parser; design §4.4) and the F1c MSS clamp
(``OSSBOT-MSS-FWD/-OUT/-IN`` in the mangle table, design §7), checked against a listing recorded from
the real iptables 1.8.10 in a scratch netns (``tests/system/capture_firewall_fixture.sh``, L-6)."""

from __future__ import annotations

import inspect
import subprocess
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
    assert list(inspect.signature(render.firewall_plan).parameters) == ["ns", "uids"]


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
def _fixture() -> tuple[str, dict[str, str], dict[tuple[str, str], str]]:
    ns, uids, sections = "", {}, {}
    key: tuple[str, str] | None = None
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            family, table = line[3:].split()
            key = (family, table)
            sections[key] = ""
        elif line.startswith("# ns "):
            ns = line.split()[2]
        elif line.startswith("# uid "):
            _, _, user, uid = line.split()
            uids[user] = uid
        elif key is not None:
            sections[key] += line + "\n"
    return ns, uids, sections


def test_check_matches_recorded_listing() -> None:
    """L-6: check_chain reports nothing for every chain of the plan against what iptables 1.8.10
    (nf_tables) printed after applying the rendered script in a scratch netns."""
    ns, uids, sections = _fixture()
    assert ns and set(uids) == set(render.SERVICE_USERS)
    chains = render.select(render.firewall_plan(ns, uids), "all")
    assert {(c.family, c.table) for c in chains} <= set(sections)
    for c in chains:
        listing = sections[(c.family, c.table)]
        assert f"-N {c.name}\n" in listing, (c.family, c.table, c.name)
        assert render.check_chain(c, listing, listing) == [], (c.family, c.table, c.name)
    assert not render.resolver_drift(sections[("iptables", "filter")], ns)


def test_recorded_listing_shows_drift_once_changed() -> None:
    ns, uids, sections = _fixture()
    [c] = [c for c in render.firewall_plan(ns, uids)["host"] if c.name == "OSSBOT-MSS-OUT"]
    listing = sections[("iptables", "mangle")].replace("--set-mss 1300", "--set-mss 1400")
    assert render.check_chain(c, listing, listing)


# ------------------------------------------------------------------------------- doctor
@pytest.mark.parametrize(("value", "status"), [("1\n", "PASS"), ("0\n", "WARN"), ("2\n", "WARN"), (None, "WARN")])
def test_doctor_warns_when_host_plpmtud_is_off(tmp_path: Path, value: str | None, status: str) -> None:
    sysctl = tmp_path / "tcp_mtu_probing"
    if value is not None:
        sysctl.write_text(value)
    result: Any = doctor.check_tcp_mtu_probing(sysctl)
    assert result.status == status and result.name == "tcp_mtu_probing"
    assert status == "PASS" or "expected 1" in result.detail
