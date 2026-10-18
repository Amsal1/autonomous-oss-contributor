"""Control flags that pick a mode fail closed (DESIGN §11.5 egress mode)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ossbot.common.control_flags import EGRESS_MODE_FILE, FLAG_MAX_BYTES, read_egress_mode, write_flag


@pytest.mark.parametrize("mode", ["allowlist", "research", "open"])
def test_egress_mode_from_the_flag(tmp_path: Path, mode: str) -> None:
    write_flag(tmp_path / EGRESS_MODE_FILE, {"mode": mode, "actor": "t", "ts": "x"})
    assert read_egress_mode(tmp_path, "open") == mode


@pytest.mark.parametrize("default", ["allowlist", "research", "open"])
def test_missing_flag_means_the_snapshot_default(tmp_path: Path, default: str) -> None:
    assert read_egress_mode(tmp_path, default) == default  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{not json",
        b"[]",
        b'"open"',
        b"{}",
        b'{"mode": "OPEN"}',
        b'{"mode": "web"}',
        b'{"mode": ["open"]}',
        b'{"mode": null}',
        b"\xff\xfe",
        b'{"mode": "open", "pad": "' + b"x" * FLAG_MAX_BYTES + b'"}',  # oversize
    ],
)
def test_unusable_flag_is_allowlist(tmp_path: Path, content: bytes) -> None:
    (tmp_path / EGRESS_MODE_FILE).write_bytes(content)
    assert read_egress_mode(tmp_path, "open") == "allowlist"


def test_symlinked_or_unreadable_flag_is_allowlist(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    write_flag(target, {"mode": "open"})
    (tmp_path / EGRESS_MODE_FILE).symlink_to(target)
    assert read_egress_mode(tmp_path, "open") == "allowlist"
    (tmp_path / EGRESS_MODE_FILE).unlink()
    (tmp_path / EGRESS_MODE_FILE).mkdir()  # a directory in place of the flag
    assert read_egress_mode(tmp_path, "open") == "allowlist"
    if os.geteuid() != 0:
        (tmp_path / EGRESS_MODE_FILE).rmdir()
        write_flag(tmp_path / EGRESS_MODE_FILE, {"mode": "open"})
        os.chmod(tmp_path / EGRESS_MODE_FILE, 0)
        assert read_egress_mode(tmp_path, "open") == "allowlist"
