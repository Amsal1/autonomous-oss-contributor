"""The public-address floor files (build-env design §4.7, AC 22): the state model's limits, ``known`` recomputed
by the reader, missing/invalid = unknown, public.txt's validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ossbot.common import public_floor as pf

pytestmark = pytest.mark.unit
NOW = 1_792_600_000.0


def _state(**over: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "schema": 1, "known": True, "last_v4_success": NOW - 30, "generation": 3,
        "v4": [{"ip": "9.9.9.9", "first_seen": NOW - 900, "last_seen": NOW - 30}], "v6": [],
    }  # fmt: skip
    doc.update(over)
    return doc


def _write(d: Path, doc: object) -> None:
    (d / pf.STATE_FILE).write_text(json.dumps(doc))


def test_paths_and_sentinels() -> None:
    assert Path("/etc/ossbot/proxy/floor") == pf.FLOOR_DIR
    assert pf.public_text([]) == "240.0.0.1/32\n"
    assert pf.public_text(["203.0.113.7/32"]) == "240.0.0.1/32\n203.0.113.7/32\n"
    assert pf.code_web_text(True) == "10.213.0.0/18\n" and pf.code_web_text(False) == "255.255.255.255/32\n"


def test_known_is_recomputed_by_the_reader(tmp_path: Path) -> None:
    _write(tmp_path, _state())
    s = pf.read_state(tmp_path, now=NOW)
    assert (s.known, s.entries, s.generation, s.valid, s.since) == (True, 1, 3, True, None)
    s = pf.read_state(tmp_path, now=NOW - 30 + pf.KNOWN_S + 1)  # stored "known": true is not trusted
    assert not s.known and s.since == NOW - 30 + pf.KNOWN_S
    _write(tmp_path, _state(known=False))
    assert pf.read_state(tmp_path, now=NOW).known  # nor is a stored false
    _write(tmp_path, _state(v4=[]))
    assert not pf.read_state(tmp_path, now=NOW).known  # no entry: never known


@pytest.mark.parametrize(
    "doc",
    [
        "not json", [], {"schema": 2}, _state(extra=1),
        _state(v4=[{"ip": "10.0.0.1", "first_seen": 1, "last_seen": 1}]),
        _state(v4=[{"ip": "100.64.0.1", "first_seen": 1, "last_seen": 1}]),
        _state(v4=[{"ip": "2001:4860::1", "first_seen": 1, "last_seen": 1}]),
        _state(v6=[{"ip": "9.9.9.9", "first_seen": 1, "last_seen": 1}]),
        _state(v4=[{"ip": "203.0.113.7", "first_seen": 1, "last_seen": 1}]),  # documentation range: not global
        _state(v4=[{"ip": f"8.8.8.{i}", "first_seen": 1, "last_seen": 1} for i in range(9)]),
        _state(last_v4_success=-1), _state(generation=-1),
    ],
)  # fmt: skip
def test_missing_or_invalid_state_reads_as_unknown(
    tmp_path: Path, doc: object, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / pf.STATE_FILE).write_text(doc if isinstance(doc, str) else json.dumps(doc))
    with caplog.at_level("WARNING"):
        s = pf.read_state(tmp_path, now=NOW)
    assert (s.known, s.valid, s.entries) == (False, False, 0)
    assert any(getattr(r, "event", None) == "public_floor_state_invalid" for r in caplog.records)
    assert "9.9.9.9" not in caplog.text and "8.8.8." not in caplog.text  # never the value


def test_missing_state_is_unknown_without_a_warning(tmp_path: Path) -> None:
    s = pf.read_state(tmp_path, now=NOW)
    assert (s.known, s.valid, s.as_dict()) == (False, False, {"known": False, "since": None, "entries": 0})


def test_state_round_trips_with_the_schema_alias() -> None:
    state = pf.PublicFloorState.model_validate(_state())
    assert json.loads(state.to_json())["schema"] == 1
    with pytest.raises(ValidationError):
        pf.PublicFloorState.model_validate(_state(schema=2))


def test_public_text_validation() -> None:
    assert pf.parse_public_text("240.0.0.1/32\n203.0.113.7/32\n2001:db8::7/128\n") == ["203.0.113.7/32",
                                                                                       "2001:db8::7/128"]  # fmt: skip
    for bad in ("203.0.113.7/32\n", "240.0.0.1/32\n203.0.113.0/24\n", "240.0.0.1/32\nnope\n",
                "240.0.0.1/32\n" + "".join(f"8.8.{i}.1/32\n" for i in range(17))):  # fmt: skip
        with pytest.raises(ValueError):
            pf.parse_public_text(bad)


def test_read_public_missing_is_empty(tmp_path: Path) -> None:
    assert pf.read_public(tmp_path) == []
    (tmp_path / pf.PUBLIC_FILE).write_text("240.0.0.1/32\n198.51.100.9/32\n")
    assert pf.read_public(tmp_path) == ["198.51.100.9/32"]
