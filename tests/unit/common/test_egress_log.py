"""The squid ``ossbot`` log format and per-job usage (open-discovery design §13.1, §13.2)."""

from __future__ import annotations

import pytest

from ossbot.common.egress_log import SANDBOX_PORT, JobWindow, Usage, aggregate, host_of, parse_line

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


def test_top_hosts_capped_at_ten() -> None:
    u = Usage()
    for i in range(15):
        e = parse_line(line(url=f"h{i:02d}.example:443"))
        assert e is not None
        u.add(e)
    assert u.top_hosts() == [f"h{i:02d}.example" for i in range(10)]
