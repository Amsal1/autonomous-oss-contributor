"""Hermes-side probe code origin and drift (DESIGN §15.2 R4-M2; PLAN item 35)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ossbot.ctl import hermes_probe as hp

REPO = Path(__file__).resolve().parents[3]
VENDOR = REPO / "vendor" / "hermes-kiro-provider"


def _tree(root: Path) -> Path:
    (root / "kiro_acp").mkdir(parents=True)
    (root / "kiro_acp" / "v3_core.py").write_text("X = 1\n", encoding="utf-8")
    (root / "__init__.py").write_text("", encoding="utf-8")
    return root


def test_tree_hash_ignores_bytecode_and_detects_changes(tmp_path: Path) -> None:
    a, b = _tree(tmp_path / "a"), _tree(tmp_path / "b")
    assert hp.tree_sha256(a) == hp.tree_sha256(b)
    (b / "kiro_acp" / "__pycache__").mkdir()
    (b / "kiro_acp" / "__pycache__" / "v3_core.cpython-314.pyc").write_bytes(b"\0")
    assert hp.tree_sha256(a) == hp.tree_sha256(b)
    (b / "kiro_acp" / "v3_core.py").write_text("X = 2\n", encoding="utf-8")
    assert hp.tree_sha256(a) != hp.tree_sha256(b)
    (b / "extra.py").write_text("", encoding="utf-8")
    assert hp.tree_sha256(a) != hp.tree_sha256(b)


def test_links_and_special_files_are_drift(tmp_path: Path) -> None:
    root = _tree(tmp_path / "t")
    (root / "link.py").symlink_to(root / "__init__.py")
    with pytest.raises(hp.Drift):
        hp.tree_sha256(root)
    (root / "link.py").unlink()
    os.mkfifo(root / "fifo")
    with pytest.raises(hp.Drift):
        hp.tree_sha256(root)
    with pytest.raises(hp.Drift):
        hp.file_sha256(root / "fifo")
    (root / "fifo").unlink()
    target = tmp_path / "secret"
    target.write_text("s", encoding="utf-8")
    (tmp_path / "ln").symlink_to(target)
    with pytest.raises(hp.Drift):
        hp.file_sha256(tmp_path / "ln")  # O_NOFOLLOW: never hashes through a link


def test_adapter_drift_when_the_hermes_copy_differs(tmp_path: Path) -> None:
    root, deployed = _tree(tmp_path / "root"), _tree(tmp_path / "hermes")
    files = {n: tmp_path / n for n in ("agent", "perm", "settings")}
    for p in files.values():
        p.write_text(p.name, encoding="utf-8")
    key = hp.compute_key(version="2.32.0", vendor_root=root, adapter=deployed, agent=files["agent"],
                         permissions=files["perm"], settings=files["settings"])  # fmt: skip
    assert key.adapter_sha256 == hp.tree_sha256(root)
    (deployed / "kiro_acp" / "v3_core.py").write_text("X = 'evil'\n", encoding="utf-8")
    with pytest.raises(hp.Drift):
        hp.compute_key(version="2.32.0", vendor_root=root, adapter=deployed, agent=files["agent"],
                       permissions=files["perm"], settings=files["settings"])  # fmt: skip


def test_core_loads_only_from_the_given_root(tmp_path: Path) -> None:
    core = hp.load_core(VENDOR, require_root=False)
    assert Path(core.__file__).resolve() == (VENDOR / "kiro_acp" / "v3_core.py").resolve()
    if os.geteuid() != 0:
        with pytest.raises(hp.Drift):  # files owned by a non-root user are refused
            hp.load_core(VENDOR)


def test_pythonpath_is_ignored_under_isolated_mode(tmp_path: Path) -> None:
    """``python -I`` ignores PYTHONPATH and the user site: a planted ``kiro_acp`` never shadows the root copy."""
    evil = tmp_path / "evil" / "kiro_acp"
    evil.mkdir(parents=True)
    (evil / "__init__.py").write_text("raise SystemExit('planted kiro_acp imported')\n", encoding="utf-8")
    code = "import sys; print(any('evil' in p for p in sys.path))"
    out = subprocess.run([sys.executable, "-I", "-c", code], env={**os.environ, "PYTHONPATH": str(evil.parent)},
                         capture_output=True, text=True, check=True)  # fmt: skip
    assert out.stdout.strip() == "False"


def test_relay_gate_needs_a_fresh_pass() -> None:
    now = 1_800_000_000.0
    stamp = hp.datetime.fromtimestamp(now - 60, hp.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = hp.datetime.fromtimestamp(now - 3 * 3600, hp.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert hp.relay_open({"passed": True, "checked_at": stamp}, now=now)
    assert not hp.relay_open({"passed": True, "checked_at": old}, now=now)
    assert not hp.relay_open({"passed": False, "checked_at": stamp}, now=now)
    assert not hp.relay_open({"passed": "true", "checked_at": stamp}, now=now)
    assert not hp.relay_open(None, now=now)


def test_result_file_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "probes" / "hermes-kiro.json"
    hp.write_result({"passed": True, "checked_at": "2026-10-19T00:00:00Z"}, path, group="nonexistent-group")
    assert hp.read_result(path) == {"passed": True, "checked_at": "2026-10-19T00:00:00Z"}
    assert oct(path.stat().st_mode & 0o777) == "0o640"
    link = tmp_path / "link.json"
    link.symlink_to(path)
    assert hp.read_result(link) is None  # O_NOFOLLOW


def test_upgrade_lock_makes_the_probe_inconclusive(tmp_path: Path) -> None:
    import fcntl

    lock = tmp_path / "hermes-upgrade.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with hp.upgrade_lock(lock) as free:
            assert free is False
    finally:
        os.close(fd)
    with hp.upgrade_lock(lock) as free:
        assert free is True
