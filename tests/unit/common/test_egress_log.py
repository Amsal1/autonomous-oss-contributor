"""The squid ``ossbot`` log format and per-job usage (open-discovery design §13.1, §13.2)."""

from __future__ import annotations

import io
import ipaddress
import json
from pathlib import Path

import pytest

from ossbot.common import egress_log
from ossbot.common.egress_log import (
    SANDBOX_PORT,
    Entry,
    Floors,
    JobWindow,
    Usage,
    aggregate,
    floor_class,
    host_of,
    parse_ip,
    parse_line,
)

pytestmark = pytest.mark.unit

T0 = 1_792_600_000.0
NET = "10.213.0.16/28"


def line(ts: float = T0 + 5, client: str = "10.213.0.18", port: int = 3128, result: str = "TCP_TUNNEL/200",
         method: str = "CONNECT", url: str = "pypi.org:443", up: int = 100, down: int = 2000) -> str:  # fmt: skip
    return f"{ts:.3f} {client} {port} {result} {method} {url} 151.101.0.223 {up} {down} 345"


def test_fields_are_parsed() -> None:
    e = parse_line(line())
    assert e is not None
    assert (e.ts, e.client, e.port, e.result, e.status, e.method) == (T0 + 5, "10.213.0.18", 3128, "TCP_TUNNEL", 200,
                                                                      "CONNECT")  # fmt: skip
    assert (e.host, e.server, e.up, e.down, e.ms, e.denied) == ("pypi.org", "151.101.0.223", 100, 2000, 345, False)


@pytest.mark.parametrize(("method", "url", "host"), [
    ("CONNECT", "PyPI.org:443", "pypi.org"),
    ("CONNECT", "[2001:db8::1]:443", "2001:db8::1"),
    ("GET", "http://deb.debian.org/debian/dists/x", "deb.debian.org"),
    ("GET", "error:invalid-request", "?"),
])  # fmt: skip
def test_connect_target_vs_url_hostname(method: str, url: str, host: str) -> None:
    assert host_of(method, url) == host


@pytest.mark.parametrize("raw", [
    "", "garbage", line() + " extra", line().replace("TCP_TUNNEL/200", "TCP_TUNNEL/xx"),
    line().replace("10.213.0.18", "not-an-ip"), line(up=-1), line().replace(f"{T0 + 5:.3f}", "yesterday"),
])  # fmt: skip
def test_malformed_lines(raw: str) -> None:
    assert parse_line(raw) is None


def test_dash_sizes_are_zero_and_denies_flagged() -> None:
    e = parse_line(line(result="TCP_DENIED/403", method="GET", url="http://x.invalid/").replace(" 100 2000 ", " - - "))
    assert e is not None and e.up == 0 and e.down == 0 and e.denied and e.status == 403


def test_window_filters_subnet_port_and_time() -> None:
    w = JobWindow.of(NET, T0)
    assert w.port == SANDBOX_PORT
    keep = [line(), line(ts=T0 - 0.5), line(client="10.213.0.31")]
    drop = [line(client="10.213.0.32"), line(port=3129), line(ts=T0 - 2), line(client="10.213.0.15")]
    for raw in keep:
        assert w.matches(parse_line(raw)), raw  # type: ignore[arg-type]
    for raw in drop:
        assert not w.matches(parse_line(raw)), raw  # type: ignore[arg-type]


def test_aggregate_counts_hosts_including_denied_and_sums_bytes() -> None:
    lines = [
        line(url="pypi.org:443", up=10, down=1000),
        line(url="pypi.org:443", up=5, down=500),
        line(url="files.pythonhosted.org:443", up=1, down=7),
        line(result="TCP_DENIED/403", url="aGVsbG8.exfil.example:443", up=0, down=0),
        line(client="10.213.0.40", url="other.example:443", up=99, down=99),  # another job's /28
        "not a squid line",
    ]
    usage, malformed = aggregate(lines, JobWindow.of(NET, T0))
    assert malformed == 1
    assert (len(usage.hosts), usage.up, usage.down, usage.denied, usage.requests) == (3, 16, 1507, 1, 4)
    assert usage.top_hosts() == ["pypi.org", "agvsbg8.exfil.example", "files.pythonhosted.org"]
    assert usage.as_dict() == {"hosts": 3, "up_bytes": 16, "down_bytes": 1507, "denied": 1, "requests": 4,
                               "top_hosts": usage.top_hosts()}  # fmt: skip


def test_eleven_tokens_carry_the_floor_mark() -> None:
    """ADR-0025: the 11th field is %{ossbot_floor}note; ``-`` is no mark; an unknown mark is malformed; the
    10-token lines of rotated pre-ADR-0025 logs still parse (no mark)."""
    for mark in ("dst", "dst_loop", "name", "quiet", "literal", "code"):
        e = parse_line(line() + f" {mark}")
        assert e is not None and e.floor == mark
    e = parse_line(line() + " -")
    assert e is not None and e.floor is None
    e = parse_line(line())
    assert e is not None and e.floor is None
    assert parse_line(line() + " DST") is None and parse_line(line() + " dst extra") is None


FLOORS = Floors.of([], host_cidrs=["192.0.2.10/32"], public=["203.0.113.7/32"])


def _e(url: str, mark: str | None, method: str = "GET", server: str = "-", result: str = "TCP_DENIED/403") -> Entry:
    e = parse_line(line(method=method, url=url, result=result).replace(" 151.101.0.223 ", f" {server} ") + " "
                   + (mark or "-"))  # fmt: skip
    assert e is not None
    return e


@pytest.mark.parametrize(
    ("url", "mark", "net", "cls", "policy"),
    [
        ("http://floor-rfc1918.ossbot.test/", "dst", "web", "dst", "hold"),
        ("http://localtest.me/", "dst_loop", "web", "loopback", "note"),
        ("https://uploads.github.com/", "name", "base", "name", "hold"),
        ("https://api.telegram.org/", "code", "web", "code", "hold"),
        ("https://api.telegram.org/", "code", "base", "code", "note"),
        ("https://checkpoint-api.hashicorp.com/", "quiet", "web", "quiet", "note"),
        ("http://10.0.0.1/", "literal", "web", "literal", "hold"),
        ("http://192.0.2.10/", "literal", "base", "literal", "hold"),  # a live host /32
        ("http://203.0.113.7/", "literal", "web", "literal", "hold"),  # the public floor
        ("http://0x7f.1/", "literal", "web", "loopback", "note"),
        ("http://2130706433/", "literal", "web", "loopback", "note"),
        ("http://017700000001/", "literal", "web", "loopback", "note"),
        ("http://[::1]/", "literal", "web", "loopback", "note"),
        ("http://0.0.0.0/", "literal", "web", "loopback", "note"),
        ("http://[fd00::1]/", "literal", "web", "literal", "hold"),
        ("http://[::ffff:10.0.0.1]/", "literal", "web", "literal", "hold"),
        ("http://0x0a.0.0.1/", "literal", "web", "literal", "hold"),
    ],
)  # fmt: skip
def test_floor_class_rows_of_section_4_5(url: str, mark: str, net: str, cls: str, policy: str) -> None:
    hit = floor_class(_e(url, mark), FLOORS, net)  # type: ignore[arg-type]
    assert hit is not None and (hit.cls, hit.policy) == (cls, policy)


def test_floor_class_public_literal_is_an_ordinary_deny_and_no_mark_is_none() -> None:
    assert floor_class(_e("http://1.1.1.1/", "literal"), FLOORS, "web") is None  # a public address: counts as a host
    assert floor_class(_e("https://example.com/", None), FLOORS, "web") is None
    assert floor_class(_e("http://999.1.1.1/", "literal"), FLOORS, "web") is None  # not decodable: reached nothing


def test_floor_class_supplementary_server_address() -> None:
    """Review 2 M2: squid printed a floor address as %<a (any mark or none): a hold; ``-`` means no inference."""
    ok = _e("example.com:443", None, method="CONNECT", server="10.1.2.3", result="TCP_TUNNEL/200")
    hit = floor_class(ok, FLOORS, "web")
    assert hit is not None and (hit.cls, hit.policy, hit.dst, hit.public) == ("dst", "hold", "10.1.2.3", False)
    pub = floor_class(_e("x.example:443", None, method="CONNECT", server="203.0.113.7", result="TCP_TUNNEL/200"),
                      FLOORS, "base")  # fmt: skip
    assert pub is not None and (pub.cls, pub.policy, pub.public) == ("pub", "hold", True)
    assert floor_class(_e("example.com:443", None, method="CONNECT", server="151.101.0.223"), FLOORS, "web") is None
    assert floor_class(_e("example.com:443", None, method="CONNECT", server="-"), FLOORS, "web") is None
    loop = floor_class(_e("example.com:443", None, method="CONNECT", server="127.0.0.1"), FLOORS, "web")
    assert loop is None  # loopback server addresses never infer a floor


def test_floors_union_the_built_ins_and_split_loopback() -> None:
    f = Floors.of(["198.51.100.0/24"])
    assert f.is_floor(ipaddress.ip_address("198.51.100.4"))  # config adds
    assert f.is_floor(ipaddress.ip_address("169.254.169.254")) and f.is_floor(ipaddress.ip_address("100.64.0.1"))
    assert f.is_floor(ipaddress.ip_address("ff02::1")) and f.is_floor(ipaddress.ip_address("64:ff9b::a00:1"))
    assert f.is_loop(ipaddress.ip_address("127.0.0.1")) and not f.is_floor(ipaddress.ip_address("127.0.0.1"))
    assert not f.is_floor(ipaddress.ip_address("1.1.1.1"))
    assert parse_ip("0x7f.1") == ipaddress.ip_address("127.0.0.1") and parse_ip("example.com") is None


def test_classify_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    public = tmp_path / "public.txt"
    public.write_text("240.0.0.1/32\n203.0.113.7/32\n")
    stdin = "\n".join([line(url="http://10.0.0.1/", method="GET", result="TCP_DENIED/403") + " literal",
                       line() + " -", "garbage"]) + "\n"  # fmt: skip
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert egress_log.main(["classify", "--net", "web", "--public-file", str(public)]) == 0
    out = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert out[0]["class"] == "literal" and out[0]["policy"] == "hold" and out[0]["mark"] == "literal"
    assert out[1]["class"] is None and out[2] == {"malformed": True}


def test_top_hosts_capped_at_ten() -> None:
    u = Usage()
    for i in range(15):
        e = parse_line(line(url=f"h{i:02d}.example:443"))
        assert e is not None
        u.add(e)
    assert u.top_hosts() == [f"h{i:02d}.example" for i in range(10)]
