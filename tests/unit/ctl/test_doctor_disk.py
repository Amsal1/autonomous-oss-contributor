"""Doctor disk-usage line (DESIGN §18.10, §18.3): WARN at or above ``retention.disk_warn_gib`` of the
snapshot (the schema default without one), with the last ``ossctl gc`` run or "never"."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ossbot.common.snapshot import write_snapshot
from ossbot.ctl import doctor

GIB = 1024**3


def _used(monkeypatch: pytest.MonkeyPatch, gib: float) -> None:
    blocks = 1000 * GIB // 4096
    used = int(gib * GIB) // 4096
    fake = SimpleNamespace(f_blocks=blocks, f_bfree=blocks - used, f_frsize=4096)
    monkeypatch.setattr(os, "statvfs", lambda path: fake)


def test_pass_below_the_default_threshold(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _used(monkeypatch, 21.0)
    r = doctor.check_disk_usage(snapshot_root=tmp_path / "none", last=tmp_path / "none.json")
    assert r.status == doctor.PASS and r.name == "disk usage"
    assert "uses 21.0 GiB (WARN at 40 GiB)" in r.detail


def test_warn_at_the_snapshot_threshold(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    write_snapshot(tmp_path / "snap", {"policy.yaml": b"version: 1\nretention:\n  disk_warn_gib: 20\n"})
    _used(monkeypatch, 20.0)
    r = doctor.check_disk_usage(snapshot_root=tmp_path / "snap", last=tmp_path / "none.json")
    assert r.status == doctor.WARN and "WARN at 20 GiB" in r.detail
    assert r.hint == "sudo ossctl gc --dry-run"


def test_detail_shows_the_last_gc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _used(monkeypatch, 1.0)
    last = tmp_path / "last.json"
    last.write_text(json.dumps({"ts": "2026-10-21T04:31:02Z", "dry_run": True, "stopped": None, "stores": {}}))
    r = doctor.check_disk_usage(snapshot_root=tmp_path / "none", last=last)
    assert r.detail.endswith("last gc 2026-10-21T04:31:02Z (dry run)")


def test_never_when_gc_has_not_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _used(monkeypatch, 1.0)
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    for path in (tmp_path / "absent.json", bad):
        assert doctor.check_disk_usage(snapshot_root=tmp_path / "none", last=path).detail.endswith("last gc never")
