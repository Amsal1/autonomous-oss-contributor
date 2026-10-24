"""ossbot-pubip (build-env design §4.7, AC 22, carried criterion C-M1): providers, validation, union, expiry,
the cap, ``known``, make-before-break on OSSBOT-PUBFLOOR, the file order, SIGHUP, the root-only lock, and two
concurrent updaters that serialise. curl, iptables and docker are stubbed; the lock runs for real on a tmp dir
owned by the test user (the only override, by argument)."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from ossbot.common import public_floor as pf
from ossbot.common.floor_lock import FloorLockUntrusted
from ossbot.ctl import pubip, render

pytestmark = pytest.mark.unit
NOW = 1_792_600_000.0
ME = {"lock_owner": os.geteuid(), "lock_group": os.getegid()}
A, B, C = "9.9.9.9", "8.8.4.4", "1.0.0.1"  # global addresses (never the owner's)


class Kernel:
    """A stubbed ``iptables -w`` on OSSBOT-PUBFLOOR: -C/-A/-D against a rule list, with an event log."""

    def __init__(self, events: list[str] | None = None, fail_on: str | None = None) -> None:
        self.rules: list[tuple[str, ...]] = []
        self.events = events if events is not None else []
        self.fail_on = fail_on
        self.lock = threading.Lock()

    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        assert args[:2] == ("-t", "filter") and args[3] == render.PUBFLOOR_CHAIN, args
        op, rule = args[2], tuple(args[4:])
        with self.lock:
            dst = rule[rule.index("-d") + 1]
            if op == "-C":
                return subprocess.CompletedProcess(args, 0 if rule in self.rules else 1, "", "")
            self.events.append(f"ipt {op} {dst}")
            if self.fail_on == op:
                return subprocess.CompletedProcess(args, 4, "", "boom")
            if op == "-A":
                self.rules.append(rule)
            elif op == "-D":
                self.rules.remove(rule)
            return subprocess.CompletedProcess(args, 0, "", "")

    def dsts(self) -> list[str]:
        out: list[str] = []
        for r in self.rules:
            d = r[r.index("-d") + 1]
            if d not in out:
                out.append(d)
        return out


@pytest.fixture
def floor(tmp_path: Path) -> Path:
    d = tmp_path / "floor"
    d.mkdir()
    os.chmod(d, 0o755)  # noqa: S103 - the live floor directory's mode (root-only writable, world-readable)
    return d


def _update(
    floor: Path,
    v4: set[str],
    v6: set[str] | None = None,
    now: float = NOW,
    kernel: Kernel | None = None,
    hups: list[str] | None = None,
) -> pubip.Outcome:
    k = kernel if kernel is not None else Kernel()
    seen = hups if hups is not None else []
    return pubip.update(floor, set(v4), set(v6 or ()), now, ipt=k, hup=lambda: seen.append("hup"), **ME)


def _files(floor: Path) -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted(floor.iterdir()) if not p.name.startswith(".")}


def _state(floor: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((floor / pf.STATE_FILE).read_text())
    return data


# ---------------------------------------------------------------------------- providers
def test_curl_argv_is_exact() -> None:
    cf, aws, v6 = pubip.PROVIDERS
    opts = ["-sS", "--noproxy", "*", "--proto", "=https", "--tlsv1.2", "--max-time", "10", "--max-filesize", "4096",
            "-A", "ossbot-pubip"]  # fmt: skip
    assert cf.argv() == ["/usr/bin/curl", *opts, "https://1.1.1.1/cdn-cgi/trace"]
    assert aws.argv() == ["/usr/bin/curl", *opts, "https://checkip.amazonaws.com/"]
    assert v6.argv() == ["/usr/bin/curl", "-6", *opts, "https://[2606:4700:4700::1111]/cdn-cgi/trace"]


@pytest.mark.parametrize(
    ("body", "provider", "want"),
    [
        (b"fl=1\nh=1.1.1.1\nip=9.9.9.9\nts=1\n", 0, A),
        (b"9.9.9.9\n", 1, A),
        (b"ip=10.0.0.5\n", 0, None),  # private
        (b"100.64.1.2\n", 1, None),  # CGNAT
        (b"ip=203.0.113.7\n", 0, None),  # documentation range: not global
        (b"ip=9.9.9.9\nip=8.8.4.4\n", 0, None),  # two ip= lines: never guess
        (b"<html>9.9.9.9</html>", 1, None),  # garbage
        (b"9" * 65, 1, None),
        (b"x" * 5000, 0, None),  # oversize
        (b"ip=2606:4700:4700::1111\n", 0, None),  # the IPv4 provider answered IPv6
        (b"ip=2606:4700:4700::1111\n", 2, "2606:4700:4700::1111"),
        (b"\xff\xfe", 1, None),
    ],
)  # fmt: skip
def test_answer_validation(body: bytes, provider: int, want: str | None) -> None:
    assert pubip.parse_answer(body, pubip.PROVIDERS[provider]) == want


def test_bad_answers_are_logged_without_the_value(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    answers = {pubip.PROVIDERS[0].url: (0, b"ip=10.9.8.7\n"), pubip.PROVIDERS[1].url: (0, b"9.9.9.9\n"),
               pubip.PROVIDERS[2].url: (7, b"")}  # fmt: skip

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        rc, out = answers[argv[-1]]
        return subprocess.CompletedProcess(argv, rc, out, b"")

    monkeypatch.setattr(pubip.subprocess, "run", run)
    with caplog.at_level("WARNING"):
        assert pubip.lookup_all() == ({A}, set())
    assert any(getattr(r, "event", None) == "pubip_bad_answer" for r in caplog.records)
    assert "10.9.8.7" not in caplog.text and A not in caplog.text
    assert not any(getattr(r, "event", None) == "pubip_lookup_failed" for r in caplog.records)  # IPv6: quiet


# -------------------------------------------------------------------------- union, known
def test_both_agree_then_disagree_union_then_one_fails(floor: Path) -> None:
    k = Kernel()
    _update(floor, {A}, kernel=k)
    assert pf.parse_public_text(_files(floor)[pf.PUBLIC_FILE]) == [f"{A}/32"]
    _update(floor, {A, B}, now=NOW + 60, kernel=k)  # multi-WAN: both floored
    assert pf.parse_public_text(_files(floor)[pf.PUBLIC_FILE]) == [f"{A}/32", f"{B}/32"]
    _update(floor, {B}, now=NOW + 120, kernel=k)  # one provider failed: the other's answer, A kept (24 h)
    assert pf.parse_public_text(_files(floor)[pf.PUBLIC_FILE]) == [f"{A}/32", f"{B}/32"]
    assert k.dsts() == [f"{A}/32", f"{B}/32"]  # the chain order = the file order (--check compares it)
    assert _state(floor)["known"] is True


def test_both_fail_keeps_the_state_and_known_lapses_after_300_s(floor: Path) -> None:
    hups: list[str] = []
    _update(floor, {A}, hups=hups)
    assert _files(floor)[pf.CODE_WEB_FILE] == "10.213.0.0/18\n" and hups == ["hup"]
    out = _update(floor, set(), now=NOW + 299, hups=hups)
    assert out.known and not out.changed and hups == ["hup"]  # timestamps only, no reload
    assert _files(floor)[pf.CODE_WEB_FILE] == "10.213.0.0/18\n"
    files = _files(floor)
    out = _update(floor, set(), now=NOW + 301, hups=hups)
    assert not out.known and out.changed and hups == ["hup", "hup"]
    after = _files(floor)
    assert after[pf.CODE_WEB_FILE] == "255.255.255.255/32\n"  # fail closed
    assert after[pf.PUBLIC_FILE] == files[pf.PUBLIC_FILE]  # known -> unknown writes only code-web.txt
    assert _state(floor)["v4"][0]["ip"] == A and _state(floor)["last_v4_success"] == NOW


def test_unknown_to_known_logs_no_value(floor: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO"):
        _update(floor, {A})
        _update(floor, set(), now=NOW + 400)
    assert any(getattr(r, "event", None) == "public_floor_unknown" for r in caplog.records)
    assert A not in caplog.text


def test_24_h_expiry_and_the_8_entry_cap(floor: Path) -> None:
    k = Kernel()
    _update(floor, {A}, kernel=k)
    _update(floor, {B}, now=NOW + pf.KEEP_S + 1, kernel=k)
    assert pf.parse_public_text(_files(floor)[pf.PUBLIC_FILE]) == [f"{B}/32"]  # A not seen for 24 h
    assert k.dsts() == [f"{B}/32"]
    many = [f"8.8.{i}.1" for i in range(10)]
    for i, ip in enumerate(many):
        _update(floor, {ip}, now=NOW + pf.KEEP_S + 10 + i, kernel=k)
    kept = pf.parse_public_text(_files(floor)[pf.PUBLIC_FILE])
    assert kept == [f"{ip}/32" for ip in many[2:]] and len(kept) == pf.MAX_PER_FAMILY  # oldest out first
    assert k.dsts() == kept


def test_ipv6_is_squid_only(floor: Path) -> None:
    k = Kernel()
    _update(floor, {A}, {"2606:4700:4700::1111"}, kernel=k)
    assert pf.parse_public_text(_files(floor)[pf.PUBLIC_FILE]) == [f"{A}/32", "2606:4700:4700::1111/128"]
    assert k.dsts() == [f"{A}/32"]


# ---------------------------------------------------------------------- order and failure
def test_make_before_break_then_files_then_hup(floor: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    real = pubip.write_file
    monkeypatch.setattr(pubip, "write_file", lambda p, t: (events.append(f"write {p.name}"), real(p, t))[1])
    k = Kernel(events)
    pubip.update(floor, {A}, set(), NOW, ipt=k, hup=lambda: events.append("hup"), **ME)
    events.clear()
    pubip.update(floor, {B}, set(), NOW + pf.KEEP_S + 1, ipt=k, hup=lambda: events.append("hup"), **ME)
    adds = [i for i, e in enumerate(events) if e == f"ipt -A {B}/32"]
    dels = [i for i, e in enumerate(events) if e == f"ipt -D {A}/32"]
    assert len(adds) == len(dels) == 6 and max(adds) < min(dels)  # the union at every moment
    tail = [e for e in events if not e.startswith("ipt ")]
    assert tail == ["write public.txt", "write code-web.txt", "write state.json", "hup"]
    assert max(dels) < events.index("write public.txt")
    assert _state(floor)["generation"] == 2


def test_no_change_rewrites_only_the_state(floor: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _update(floor, {A})
    events: list[str] = []
    real = pubip.write_file
    monkeypatch.setattr(pubip, "write_file", lambda p, t: (events.append(p.name), real(p, t))[1])
    k = Kernel(events)
    hups: list[str] = []
    out = _update(floor, {A}, now=NOW + 60, kernel=k, hups=hups)
    assert not out.changed and events == ["state.json"] and hups == []
    assert _state(floor)["generation"] == 1 and _state(floor)["last_v4_success"] == NOW + 60


@pytest.mark.parametrize("op", ["-A", "-D"])
def test_iptables_failure_leaves_the_three_files_untouched(floor: Path, op: str) -> None:
    k = Kernel()
    _update(floor, {A}, kernel=k)
    before = _files(floor)
    k.fail_on = op
    hups: list[str] = []
    with pytest.raises(pubip.KernelError):
        _update(floor, {B}, now=NOW + pf.KEEP_S + 1, kernel=k, hups=hups)
    assert _files(floor) == before and hups == []


def test_main_exits_1_and_logs_no_value_on_a_kernel_error(floor: Path, monkeypatch: pytest.MonkeyPatch,
                                                         caplog: pytest.LogCaptureFixture) -> None:  # fmt: skip
    monkeypatch.setattr(pubip, "lookup_all", lambda: ({A}, set()))
    real = pubip.update

    def update(floor_dir: Path, v4: set[str], v6: set[str], now: float) -> pubip.Outcome:
        return real(floor_dir, v4, v6, now, ipt=Kernel(fail_on="-A"), hup=lambda: None, **ME)

    monkeypatch.setattr(pubip, "update", update)
    with caplog.at_level("INFO"):
        assert pubip.main(["--floor-dir", str(floor)]) == 1
    assert any(getattr(r, "event", None) == "pubip_update_failed" for r in caplog.records)
    assert A not in caplog.text and not list(floor.glob("*.txt"))


# -------------------------------------------------------------------------- lock (C-M1)
def test_the_update_holds_the_lock_exclusively(floor: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    taken: list[tuple[bool, Path]] = []
    real = pubip.floor_lock

    def spy(exclusive: bool, timeout_s: float, path: Path, **kw: int) -> Any:
        taken.append((exclusive, path))
        return real(exclusive, timeout_s, path, **kw)

    monkeypatch.setattr(pubip, "floor_lock", spy)
    _update(floor, {A})
    assert taken == [(True, floor / ".lock")]


@pytest.mark.parametrize("case", ["other_uid", "mode_0644", "symlink"])
def test_an_untrusted_lock_is_refused_and_the_files_stay_untouched(
    floor: Path, case: str, caplog: pytest.LogCaptureFixture
) -> None:
    _update(floor, {A})
    before = _files(floor)
    lock = floor / ".lock"
    owner = ME
    if case == "mode_0644":
        os.chmod(lock, 0o644)
    elif case == "symlink":
        lock.unlink()
        (floor.parent / "planted").write_text("")
        os.chmod(floor.parent / "planted", 0o600)
        lock.symlink_to(floor.parent / "planted")
    else:  # a lock file owned by another uid than the one we trust
        owner = {"lock_owner": os.geteuid() + 1, "lock_group": os.getegid()}
    hups: list[str] = []
    with caplog.at_level("ERROR"), pytest.raises(FloorLockUntrusted):
        pubip.update(floor, {B}, set(), NOW + pf.KEEP_S + 1, ipt=Kernel(), hup=lambda: hups.append("hup"), **owner)
    assert any(getattr(r, "event", None) == "floor_lock_untrusted" for r in caplog.records)
    assert _files(floor) == before and hups == []


def test_two_concurrent_updaters_serialise_and_end_consistent(floor: Path) -> None:
    """C-M1: the critical sections never overlap, and the files and the chain agree afterwards."""
    k = Kernel()
    inside: list[int] = []
    overlaps: list[str] = []
    order: list[str] = []
    gate = threading.Lock()

    def hup(name: str) -> None:
        with gate:
            inside.append(1)
            if len(inside) > 1:
                overlaps.append(name)
        time.sleep(0.2)  # a slow reload inside the critical section
        with gate:
            inside.pop()
            order.append(name)

    def run(name: str, v4: set[str], now: float) -> None:
        pubip.update(floor, v4, set(), now, ipt=k, hup=lambda: hup(name), lock_timeout_s=10, **ME)

    _update(floor, {A}, kernel=k)
    threads = [threading.Thread(target=run, args=("one", {A, B}, NOW + 60)),
               threading.Thread(target=run, args=("two", {A, C}, NOW + 61))]  # fmt: skip
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not overlaps and sorted(order) == ["one", "two"]
    entries = pf.parse_public_text(_files(floor)[pf.PUBLIC_FILE])
    assert set(entries) == {f"{A}/32", f"{B}/32", f"{C}/32"}  # neither update lost the other's address
    assert k.dsts() == entries  # the chain matches the file, in order
    assert [e["ip"] for e in _state(floor)["v4"]] == [e[:-3] for e in entries]
    assert _state(floor)["generation"] == 3
