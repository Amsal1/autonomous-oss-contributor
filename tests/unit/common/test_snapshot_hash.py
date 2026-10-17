"""N2: the snapshot hash is the length-prefixed, name-sorted formula of DESIGN §7.9."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from ossbot.common.snapshot import read_current, read_snapshot, snapshot_hash, write_snapshot


def reference(files: dict[str, bytes]) -> str:
    return hashlib.sha256(
        b"".join(name.encode() + b"\0" + str(len(data)).encode() + b"\0" + data for name, data in sorted(files.items()))
    ).hexdigest()


FILES = {"policy.yaml": b"version: 1\n", "targets.yaml": b"a: 1\n", "egress.yaml": b"", "profile.yaml": b"x"}


def test_hash_equals_formula() -> None:
    assert snapshot_hash(FILES) == reference(FILES)


def test_hash_independent_of_insertion_order() -> None:
    assert snapshot_hash(dict(reversed(list(FILES.items())))) == snapshot_hash(FILES)


def test_renaming_a_file_changes_hash() -> None:
    renamed = dict(FILES)
    renamed["policy2.yaml"] = renamed.pop("policy.yaml")
    assert snapshot_hash(renamed) != snapshot_hash(FILES)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ({"a.yaml": b"xy", "b.yaml": b"z"}, {"a.yaml": b"x", "b.yaml": b"yz"}),  # bytes moved between files
        ({"a.yaml": b"", "b.yaml": b"1"}, {"a.yaml": b"1", "b.yaml": b""}),
        ({"ab": b"c"}, {"a": b"bc"}),  # name/content boundary
    ],
)
def test_moving_bytes_changes_hash(a: dict[str, bytes], b: dict[str, bytes]) -> None:
    assert snapshot_hash(a) != snapshot_hash(b)


def test_write_and_read_snapshot(tmp_path: Path) -> None:
    digest = write_snapshot(tmp_path, FILES)
    assert digest == reference(FILES)
    assert os.readlink(tmp_path / "current") == digest
    for name in FILES:
        assert (tmp_path / digest / name).stat().st_mode & 0o777 == 0o444
    assert read_current(tmp_path) == (digest, FILES)
    # idempotent rewrite; current repointed to a second snapshot
    other = dict(FILES, **{"policy.yaml": b"version: 1\n# c\n"})
    second = write_snapshot(tmp_path, other)
    assert read_current(tmp_path)[0] == second
    assert read_snapshot(tmp_path, digest) == FILES


def test_tampered_snapshot_detected(tmp_path: Path) -> None:
    digest = write_snapshot(tmp_path, FILES)
    target = tmp_path / digest / "profile.yaml"
    target.chmod(0o644)
    target.write_bytes(b"y")
    with pytest.raises(ValueError, match="does not match"):
        read_snapshot(tmp_path, digest)
