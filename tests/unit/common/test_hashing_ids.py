"""Canonical JSON stability, ids, tokens, redaction, flags, clock."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ossbot.common.clock import FakeClock, from_iso, to_iso
from ossbot.common.control_flags import Blocked, read_flags, require_running, write_flag
from ossbot.common.hashing import canonical_json, canonical_sha256
from ossbot.common.ids import ID_RE, TOKEN_RE, new_id, new_token
from ossbot.common.logging import JsonFormatter
from ossbot.common.redact import Redactor

json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(), children, max_size=4),
    max_leaves=20,
)


def test_canonical_json_is_sorted_and_compact() -> None:
    assert canonical_json({"b": 1, "a": [1, {"d": "é", "c": None}]}) == '{"a":[1,{"c":null,"d":"é"}],"b":1}'.encode()


def test_canonical_json_rejects_nan() -> None:
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})


@given(json_values)
def test_canonical_json_roundtrip_stable(value: object) -> None:
    once = canonical_json(value)
    assert canonical_json(json.loads(once)) == once


def test_canonical_hash_key_order_independent() -> None:
    assert canonical_sha256({"a": 1, "b": 2}) == canonical_sha256({"b": 2, "a": 1})


def test_ids_sortable_and_well_formed() -> None:
    ids = [new_id("c", now_ms=1_700_000_000_000 + i) for i in range(50)]
    assert all(ID_RE.match(i) for i in ids)
    assert ids == sorted(ids)
    same_ms = {new_id("a", now_ms=1_800_000_000_000) for _ in range(20)}
    assert len(same_ms) == 20


def test_tokens_are_22_char_base64url() -> None:
    tokens = {new_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(TOKEN_RE.match(t) for t in tokens)


def test_redactor_patterns_and_values() -> None:
    r = Redactor(secrets=["super-secret-value"])
    text = r.redact("k=ksk_" + "a" * 20 + " t=123456789:" + "b" * 35 + " v=super-secret-value")
    assert "ksk_" not in text and "super-secret" not in text and "123456789:" not in text


def test_json_formatter_redacts() -> None:
    fmt = JsonFormatter("test", Redactor(secrets=["hunter2hunter2"]))
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "token %s", ("hunter2hunter2",), None)
    record.cid = "c_123"
    doc = json.loads(fmt.format(record))
    assert doc["msg"] == "token [REDACTED]" and doc["cid"] == "c_123" and doc["component"] == "test"


def test_clock_iso_roundtrip() -> None:
    c = FakeClock()
    assert from_iso(to_iso(c.now())) == c.now()
    c.advance(minutes=5)
    assert to_iso(c.now()).endswith("Z")


@pytest.mark.parametrize(
    ("content", "level"),
    [
        ({"level": "soft", "reason": "r"}, "soft"),
        ({"level": "full", "reason": "r"}, "full"),
        ({"reason": "no level"}, "full"),
        ("not json", "full"),
        ({"level": "weird"}, "full"),
    ],
)
def test_kill_flag_level_fail_closed(tmp_path: Path, content: object, level: str) -> None:
    (tmp_path / "killed").write_text(content if isinstance(content, str) else json.dumps(content))
    flags = read_flags(tmp_path)
    assert flags.killed is not None and flags.killed.level == level and flags.state == "killed"
    with pytest.raises(Blocked):
        require_running(tmp_path, "x")


def test_symlinked_flag_counts_as_present(tmp_path: Path) -> None:
    (tmp_path / "elsewhere").write_text("{}")
    (tmp_path / "paused").symlink_to(tmp_path / "elsewhere")
    assert read_flags(tmp_path).state == "paused"


def test_write_flag_mode(tmp_path: Path) -> None:
    write_flag(tmp_path / "paused", {"reason": "x"})
    assert (tmp_path / "paused").stat().st_mode & 0o777 == 0o640
    assert read_flags(tmp_path).paused is not None
