"""The public-address floor gate in sandboxd (build-env design §4.7, §5.1, ADR-0025): while the floor is unknown,
web fetch/validate/agent-exec jobs are refused with 503 ``public_floor_unknown`` (base and strict jobs run) and
running web jobs are stopped (``public_floor_unknown``: no hold, no note); a P1 notice after 10 minutes in a web
mode (deduped 6 h); the late check holds a job whose earlier connection's server address becomes the public
floor, as class ``pub`` with the value scrubbed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ossbot.common.config import EgressAnomaly
from ossbot.common.public_floor import FloorEntry, PublicFloorState, public_text
from ossbot.common.schemas import HoldRequest
from ossbot.sandboxd.egress_meter import EgressMeter
from ossbot.sandboxd.jobs import ApiError, floor_hold_body
from ossbot.sandboxd.profiles import JobSpec
from tests.unit.sandboxd.conftest import KNOWN, UNKNOWN, Sbx, pass_all_probes

pytestmark = pytest.mark.unit
T0 = 1_792_600_000.0
CID = "c_01ABCDEFGH"
PUB = "93.184.215.14"  # a global address standing in for the owner's
NET = "10.213.0.16/28"


def _spec(sbx: Sbx, profile: str, run_id: str, egress: str = "mode") -> JobSpec:
    body: dict[str, Any] = {"run_id": run_id, "cid": CID, "profile": profile, "entrypoint": profile,
                            "repo_mode": "rw", "timeout_s": 600, "egress": egress, "args": {}}  # fmt: skip
    if profile == "fetch":
        body["args"] = {"repo": "owner/repo"}
    if profile == "validate":
        body["inputs"] = {"validate.json": sbx.runner_input("validate.json", b'{"commands": []}')}
    if profile == "coder":
        body.update(
            {
                "entrypoint": "kiro",
                "inputs": {"code.md": sbx.runner_input("code.md", b"implement")},
                "args": {"role": "coder", "prompt_file": "code.md", "model": "claude-test", "effort": "high"},
            }
        )
    return JobSpec.model_validate(body)


@pytest.mark.parametrize("mode", ["sandbox_open", "sandbox_full"])
@pytest.mark.parametrize("profile", ["fetch", "validate", "coder"])
def test_web_jobs_are_refused_while_unknown(sbx: Sbx, mode: str, profile: str) -> None:
    pass_all_probes(sbx)
    sbx.mode[0], sbx.floor[0] = mode, UNKNOWN
    sbx.gatekeeper.egress_answer = "web"
    with pytest.raises(ApiError) as info:
        sbx.jobs.submit(_spec(sbx, profile, "r1"))
    assert (info.value.status, info.value.error_class) == (503, "public_floor_unknown")
    assert [c for c, _ in sbx.runtime.calls if c == "create"] == []  # nothing was started


@pytest.mark.parametrize(("mode", "egress"), [("sandbox_full", "strict"), ("allowlist", "mode"), ("open", "mode")])
@pytest.mark.parametrize("profile", ["fetch", "validate"])
def test_base_and_strict_jobs_still_run(sbx: Sbx, mode: str, egress: str, profile: str) -> None:
    sbx.mode[0], sbx.floor[0] = mode, UNKNOWN
    plan = sbx.jobs.admit(_spec(sbx, profile, "r1", egress=egress))
    assert plan.net == "base" and plan.env["OSSBOT_NET"] == "base"


def test_a_known_floor_admits_web_jobs(sbx: Sbx) -> None:
    sbx.mode[0], sbx.floor[0] = "sandbox_full", KNOWN
    plan = sbx.jobs.admit(_spec(sbx, "validate", "r1"))
    assert plan.net == "web" and plan.env["OSSBOT_NET"] == "web"


def test_an_unreadable_floor_is_unknown(sbx: Sbx) -> None:
    def boom() -> Any:
        raise OSError("no floor dir")

    sbx.mode[0] = "sandbox_full"
    sbx.jobs.public_floor = boom
    with pytest.raises(ApiError) as info:
        sbx.jobs.admit(_spec(sbx, "fetch", "r1"))
    assert info.value.error_class == "public_floor_unknown"


# ----------------------------------------------------------------------------------------- the meter
def _floor_dir(tmp_path: Path, *, last_ok: float, generation: int, public: list[str]) -> Path:
    d = tmp_path / "floor"
    d.mkdir(exist_ok=True)
    state = PublicFloorState(known=True, last_v4_success=last_ok, generation=generation,
                             v4=[FloorEntry(ip=PUB, first_seen=last_ok, last_seen=last_ok)])  # fmt: skip
    (d / "state.json").write_text(state.to_json(), encoding="utf-8")
    (d / "public.txt").write_text(public_text(public), encoding="utf-8")
    return d


class Rig:
    def __init__(self, tmp_path: Path, floor_dir: Path, mode: str = "sandbox_full") -> None:
        self.now = [T0]
        self.mode = [mode]
        self.squid = tmp_path / "access.log"
        self.squid.write_text("", encoding="utf-8")
        self.unknown: list[str] = []
        self.floor: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.notices: list[str] = []
        self.meter = EgressMeter(
            self.squid, tmp_path / "runs", EgressAnomaly, wall=lambda: self.now[0], sleep=lambda s: None,
            public_floor_dir=floor_dir, current_mode=lambda: self.mode[0], notify=self.notices.append,
            on_unknown=lambda r, d: self.unknown.append(r), on_floor=lambda r, d: self.floor.append(d),
            on_note=lambda r, d: self.notes.append(r),
        )  # fmt: skip
        self.meter.prime()


def test_running_web_jobs_stop_once_when_the_floor_becomes_unknown(tmp_path: Path) -> None:
    d = _floor_dir(tmp_path, last_ok=T0 - 10, generation=1, public=[f"{PUB}/32"])
    rig = Rig(tmp_path, d)
    rig.meter.register("web1", CID, "validate", NET, T0, "mode", "sandbox_full", "web")
    rig.meter.register("base1", "c_01ABCDEFGJ", "fetch", "10.213.192.16/28", T0, "strict", "sandbox_full", "base")
    rig.meter.poll()
    assert rig.unknown == [] and rig.meter.health()["public_floor"] == {"known": True, "since": None, "entries": 1}
    rig.now[0] = T0 + 400  # the last good answer is older than 300 s
    rig.meter.poll()
    rig.meter.poll()
    assert rig.unknown == ["web1"]  # once; the base job runs on
    assert rig.floor == [] and rig.notes == []  # no hold, no note
    assert rig.meter.health()["public_floor"]["known"] is False


@pytest.mark.parametrize(("mode", "noticed"), [("sandbox_full", True), ("sandbox_open", True), ("allowlist", False)])
def test_the_10_minute_notice_in_a_web_mode_deduped_6h(tmp_path: Path, mode: str, noticed: bool) -> None:
    d = _floor_dir(tmp_path, last_ok=T0 - 400, generation=1, public=[f"{PUB}/32"])  # unknown since T0 - 100
    rig = Rig(tmp_path, d, mode=mode)
    rig.meter.poll()
    assert rig.notices == []  # not before 10 minutes
    rig.now[0] = T0 + 501
    rig.meter.poll()
    rig.now[0] = T0 + 3600
    rig.meter.poll()
    expected = ["Public-address check failing for 10 min: repo code's public web is paused"] if noticed else []
    assert rig.notices == expected
    rig.now[0] = T0 + 501 + 6 * 3600
    rig.meter.poll()
    assert len(rig.notices) == (2 if noticed else 0)


def test_late_check_holds_a_connection_to_a_newly_learned_address(tmp_path: Path) -> None:
    d = _floor_dir(tmp_path, last_ok=T0, generation=1, public=[])
    rig = Rig(tmp_path, d)
    rig.meter.register("r1", CID, "validate", NET, T0 - 5, "mode", "sandbox_full", "web")
    rig.meter.register("r2", "c_01ABCDEFGJ", "validate", "10.213.0.32/28", T0 - 5, "mode", "sandbox_full", "web")
    with rig.squid.open("a", encoding="utf-8") as fh:
        fh.write(f"{T0 - 1:.3f} 10.213.0.18 3128 TCP_TUNNEL/200 CONNECT my.router.example:443 {PUB} 10 10 50 -\n")
        fh.write(f"{T0 - 1:.3f} 10.213.0.34 3128 TCP_TUNNEL/200 CONNECT elsewhere.example:443 1.1.1.1 10 10 50 -\n")
    rig.meter.poll()
    assert rig.floor == []  # not a floor yet
    _floor_dir(tmp_path, last_ok=T0 + 60, generation=2, public=[f"{PUB}/32"])  # pubip learned the address
    rig.now[0] = T0 + 60
    rig.meter.poll()
    assert len(rig.floor) == 1
    doc = rig.floor[0]
    assert doc["run_id"] == "r1" and doc["floor"] == {"class": "pub", "target": "public-address", "dst": "-"}
    assert PUB not in repr(doc)  # the value never leaves sandboxd
    body = floor_hold_body(doc)
    req = HoldRequest.model_validate(body)
    assert req.reason == "egress_floor" and req.floor.cls == "pub" and req.breached == []


def test_a_squid_literal_of_the_public_floor_is_scrubbed(tmp_path: Path) -> None:
    d = _floor_dir(tmp_path, last_ok=T0, generation=1, public=[f"{PUB}/32"])
    rig = Rig(tmp_path, d)
    rig.meter.register("r1", CID, "fetch", NET, T0 - 5, "mode", "sandbox_full", "web")
    with rig.squid.open("a", encoding="utf-8") as fh:
        fh.write(f"{T0:.3f} 10.213.0.18 3128 TCP_DENIED/403 CONNECT {PUB}:443 - 0 0 1 literal\n")
    rig.meter.poll()
    assert [f["floor"] for f in rig.floor] == [{"class": "literal", "target": "public-address", "dst": "-"}]
    doc = rig.meter.finish("r1")
    assert doc is not None and PUB not in repr(doc)
