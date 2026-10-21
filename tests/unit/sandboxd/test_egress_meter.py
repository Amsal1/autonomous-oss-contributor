"""The per-job egress meter (open-discovery design §13.2, review-3 M3): rotation without a lost line, a
missing log, the read cap, breaches by hosts/upload/download stop the job and request the hold once, the
summary file, the pending-hold fail-closed path (5xx retried; 400/404 final, rejected_code, ERROR, still
423) and strict jobs metered too."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ossbot.common.config import EgressAnomaly
from ossbot.common.schemas import HoldRequest
from ossbot.sandboxd.egress_meter import MAX_READ, EgressMeter
from ossbot.sandboxd.jobs import ApiError, HoldError
from ossbot.sandboxd.profiles import JobSpec
from tests.unit.sandboxd.conftest import Sbx

T0 = 1_792_600_000.0
NET = "10.213.0.16/28"
MIB = 1024 * 1024
LIMITS = EgressAnomaly(max_hosts=5, max_upload_mb=1, max_download_mb=64)
CID = "c_01ABCDEFGH"


def sq(ts: float, client: str, host: str, up: int = 10, down: int = 10, port: int = 3128) -> str:
    return f"{ts:.3f} {client} {port} TCP_TUNNEL/200 CONNECT {host}:443 1.2.3.4 {up} {down} 50\n"


def append(path: Path, *lines: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.writelines(lines)


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    path = tmp_path / "proxy" / "access.log"
    path.parent.mkdir()
    path.write_text(sq(T0 - 100, "10.213.0.18", "before.example"), encoding="utf-8")  # there before the start
    return path


def meter_for(path: Path, tmp_path: Path, **kw: Any) -> tuple[EgressMeter, list[tuple[str, dict[str, Any]]]]:
    seen: list[tuple[str, dict[str, Any]]] = []
    meter = EgressMeter(path, tmp_path / "runs", lambda: LIMITS, on_breach=lambda r, d: seen.append((r, d)),
                        wall=lambda: T0 + 600, sleep=lambda s: None, **kw)  # fmt: skip
    meter.prime()
    return meter, seen


def test_default_read_cap_is_8_mib() -> None:
    assert MAX_READ == 8 * 1024 * 1024


def test_rotation_across_a_rename_loses_no_line(log_path: Path, tmp_path: Path) -> None:
    meter, _ = meter_for(log_path, tmp_path)
    meter.register("r1", CID, "validate", NET, T0, "mode", "sandbox_open")
    append(log_path, sq(T0 + 1, "10.213.0.18", "a.example"))
    meter.poll()
    os.rename(log_path, log_path.with_name("access.log.20261021T001000Z"))  # ossbot-proxy-rotate
    append(log_path.with_name("access.log.20261021T001000Z"), sq(T0 + 2, "10.213.0.18", "b.example"))  # still squid's
    append(log_path, sq(T0 + 3, "10.213.0.18", "c.example"))  # squid -k rotate reopened the path
    meter.poll()
    meter.poll()
    doc = meter.finish("r1")
    assert doc is not None and doc["hosts"] == 3 and doc["requests"] == 3  # before.example was skipped
    assert sorted(doc["top_hosts"]) == ["a.example", "b.example", "c.example"]


def test_a_missing_log_is_read_once_it_exists(tmp_path: Path) -> None:
    path = tmp_path / "proxy" / "access.log"
    meter, _ = meter_for(path, tmp_path)
    meter.register("r1", CID, "fetch", NET, T0, "mode", "open")
    meter.poll()  # nothing there: no error
    path.parent.mkdir()
    append(path, sq(T0 + 1, "10.213.0.20", "late.example"))
    meter.poll()
    doc = meter.finish("r1")
    assert doc is not None and doc["top_hosts"] == ["late.example"]


def test_each_poll_reads_at_most_the_cap(log_path: Path, tmp_path: Path) -> None:
    lines = [sq(T0 + 1, "10.213.0.18", f"h{i}.example") for i in range(6)]
    meter, _ = meter_for(log_path, tmp_path, max_read=len(lines[0]) + 5)
    meter.register("r1", CID, "fetch", NET, T0, "mode", "open")
    append(log_path, *lines)
    meter.poll()
    assert len(meter._jobs["r1"].usage.hosts) == 1
    for _ in range(10):
        meter.poll()
    assert len(meter._jobs["r1"].usage.hosts) == 6


@pytest.mark.parametrize(("what", "lines"), [
    ("hosts", [sq(T0 + 1, "10.213.0.18", f"h{i}.example") for i in range(6)]),
    ("upload", [sq(T0 + 1, "10.213.0.18", "paste.example", up=2 * MIB)]),
    ("download", [sq(T0 + 1, "10.213.0.18", "big.example", down=65 * MIB)]),
])  # fmt: skip
def test_breach_is_reported_once(log_path: Path, tmp_path: Path, what: str, lines: list[str]) -> None:
    meter, seen = meter_for(log_path, tmp_path)
    meter.register("r1", CID, "validate", NET, T0, "mode", "sandbox_open")
    append(log_path, *lines)
    meter.poll()
    meter.poll()
    append(log_path, sq(T0 + 2, "10.213.0.18", "more.example", up=5 * MIB))
    meter.poll()
    meter.finish("r1")
    assert [(r, d["breached"][0]) for r, d in seen] == [("r1", what)]


def test_other_subnets_and_ports_do_not_count(log_path: Path, tmp_path: Path) -> None:
    meter, seen = meter_for(log_path, tmp_path)
    meter.register("r1", CID, "fetch", NET, T0, "mode", "open")
    append(log_path, sq(T0 + 1, "10.213.0.40", "x.example", up=9 * MIB), sq(T0 + 1, "10.213.0.18", "y", port=3200))
    doc = meter.finish("r1")
    assert doc is not None and doc["requests"] == 0 and seen == []


def test_under_threshold_summary_file(log_path: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    meter, seen = meter_for(log_path, tmp_path)
    meter.register("r1", CID, "fetch", NET, T0, "strict", "open")
    append(log_path, sq(T0 + 1, "10.213.0.18", "github.com", up=100, down=5000))
    with caplog.at_level(logging.INFO):
        doc = meter.finish("r1")
    assert seen == [] and doc is not None and doc["breached"] == []
    path = tmp_path / "runs" / "r1.egress.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored == doc
    assert (stored["egress"], stored["egress_mode"], stored["subnet"], stored["up_bytes"]) == ("strict", "open", NET,
                                                                                               100)  # fmt: skip
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o640
    assert any(getattr(r, "event", "") == "egress_usage" for r in caplog.records)
    assert meter.finish("r1") is None  # unregistered


def test_unreadable_limits_use_the_defaults(log_path: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    def broken() -> EgressAnomaly:
        raise OSError("no snapshot")

    meter = EgressMeter(log_path, tmp_path / "runs", broken, sleep=lambda s: None)
    with caplog.at_level(logging.WARNING):
        meter.register("r1", CID, "fetch", NET, T0, "mode", "open")
    assert meter._jobs["r1"].limits == EgressAnomaly()
    assert any(getattr(r, "event", "") == "egress_limits_default" for r in caplog.records)


# ----------------------------------------------------------------------- with JobService
def _job(kind: str, run_id: str, egress: str = "mode", sbx: Sbx | None = None) -> JobSpec:
    args = {"repo": "owner/repo"} if kind == "fetch" else {}
    body: dict[str, Any] = {"run_id": run_id, "cid": CID, "profile": kind, "entrypoint": kind, "repo_mode": "rw",
                            "timeout_s": 600, "egress": egress, "args": args}  # fmt: skip
    if kind == "validate":
        assert sbx is not None
        body["inputs"] = {"validate.json": sbx.runner_input("validate.json", b'{"commands": []}')}
    return JobSpec.model_validate(body)


def _metered(sbx: Sbx, log_path: Path, traffic: Callable[[str, float], list[str]]) -> EgressMeter:
    meter = EgressMeter(log_path, sbx.settings.logs_dir / "runs", lambda: LIMITS, sleep=lambda s: None,
                        on_breach=sbx.jobs.flag_anomaly)  # fmt: skip
    meter.prime()
    register = meter.register

    def with_traffic(run_id: str, cid: str, stage: str, subnet: str, start_ts: float, egress: str, mode: str) -> None:
        register(run_id, cid, stage, subnet, start_ts, egress, mode)
        client = str(next(ipaddress.IPv4Network(subnet).hosts()))
        append(log_path, *(line.replace("CLIENT", client) for line in traffic(subnet, start_ts)))

    meter.register = with_traffic  # type: ignore[method-assign]
    sbx.jobs.meter = meter
    sbx.jobs.egress_mode = lambda: "sandbox_open"
    return meter


def _upload(subnet: str, ts: float) -> list[str]:
    return [sq(ts + 1, "CLIENT", "paste.example", up=80 * MIB)]


def _quiet(subnet: str, ts: float) -> list[str]:
    return [sq(ts + 1, "CLIENT", "proxy.golang.org", up=100, down=10 * MIB)]


@pytest.mark.parametrize("kind", ["fetch", "validate"])
def test_breach_stops_the_job_and_requests_the_hold_once(sbx: Sbx, log_path: Path, kind: str) -> None:
    _metered(sbx, log_path, _upload)
    view = sbx.jobs.submit(_job(kind, "r1", sbx=sbx))
    assert (view["state"], view["error_class"], view["artifact_dir"]) == ("failed", "egress_anomaly", None)
    assert not (sbx.settings.staging_dir / "r1").exists()
    assert not (sbx.settings.artifacts_dir / CID / "runs" / "r1").exists()
    [(cid, body)] = sbx.gatekeeper.holds
    assert cid == CID
    req = HoldRequest.model_validate(body)
    assert (req.stage, req.egress, req.egress_mode, req.breached, req.top_hosts) == (
        kind, "mode", "sandbox_open", ["upload"], ["paste.example"])  # fmt: skip
    assert sbx.jobs.pending_holds() == []
    assert (sbx.settings.logs_dir / "runs" / "r1.egress.json").exists()


def test_strict_jobs_are_metered_too(sbx: Sbx, log_path: Path) -> None:
    _metered(sbx, log_path, _quiet)
    assert sbx.jobs.submit(_job("fetch", "r1", egress="strict"))["state"] == "succeeded"
    doc = json.loads((sbx.settings.logs_dir / "runs" / "r1.egress.json").read_text(encoding="utf-8"))
    assert doc["egress"] == "strict" and ipaddress.IPv4Network(doc["subnet"]).subnet_of(
        ipaddress.IPv4Network("10.213.192.0/19"))  # fmt: skip
    assert sbx.gatekeeper.holds == []


def test_a_5xx_keeps_the_cid_blocked_until_the_retry_records_it(sbx: Sbx, log_path: Path) -> None:
    _metered(sbx, log_path, _upload)
    sbx.gatekeeper.hold_answers = [HoldError(503, "internal_error"), HoldError(None, "transport")]
    sbx.jobs.submit(_job("fetch", "r1"))
    [pending] = sbx.jobs.pending_holds()
    assert pending["cid"] == CID and pending["rejected_code"] is None
    with pytest.raises(ApiError) as info:
        sbx.jobs.submit(_job("validate", "r2", sbx=sbx))
    assert (info.value.status, info.value.error_class) == (423, "egress_anomaly_pending")
    assert sbx.jobs.retry_pending_holds() == 0  # the transport error: still pending
    assert sbx.jobs.retry_pending_holds() == 1  # recorded now
    assert sbx.jobs.pending_holds() == [] and len(sbx.gatekeeper.holds) == 3
    sbx.jobs.meter = None  # the gatekeeper's hold (423 at stage open) takes over from here
    assert sbx.jobs.submit(_job("fetch", "r3"))["state"] == "succeeded"


@pytest.mark.parametrize(("status", "code"), [(400, "schema_error"), (404, "unknown_cid")])
def test_a_4xx_is_final_and_fails_closed(
    sbx: Sbx, log_path: Path, caplog: pytest.LogCaptureFixture, status: int, code: str
) -> None:
    _metered(sbx, log_path, _upload)
    sbx.gatekeeper.hold_answers = [HoldError(status, code)]
    with caplog.at_level(logging.ERROR):
        sbx.jobs.submit(_job("fetch", "r1"))
    [pending] = sbx.jobs.pending_holds()
    assert pending["rejected_code"] == f"{status}:{code}"
    assert any(getattr(r, "event", "") == "hold_rejected" for r in caplog.records)
    assert sbx.jobs.retry_pending_holds() == 0
    assert len(sbx.gatekeeper.holds) == 1  # not retried
    with pytest.raises(ApiError) as info:
        sbx.jobs.submit(_job("fetch", "r2"))
    assert (info.value.status, info.value.error_class) == (423, "egress_anomaly_pending")
    sbx.jobs.clear_pending(CID)  # ossctl egress release (root, TTY confirm)
    with pytest.raises(ApiError):
        sbx.jobs.clear_pending(CID)
    sbx.jobs.meter = None
    assert sbx.jobs.submit(_job("fetch", "r3"))["state"] == "succeeded"


def test_pending_blocks_only_repo_code_jobs_of_that_cid(sbx: Sbx, log_path: Path) -> None:
    _metered(sbx, log_path, _upload)
    sbx.gatekeeper.hold_answers = [HoldError(503, "internal_error")]
    sbx.jobs.submit(_job("fetch", "r1"))
    sbx.jobs.meter = None
    other = _job("fetch", "r2").model_copy(update={"cid": "c_01ABCDEFGJ"})
    assert sbx.jobs.submit(other)["state"] == "succeeded"
