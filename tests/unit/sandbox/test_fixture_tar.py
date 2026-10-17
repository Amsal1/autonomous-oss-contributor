"""The probe fixture tar build step keeps its symlinks (DESIGN §10.1, §10.9)."""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_build_probe_tar_preserves_symlinks(tmp_path: Path) -> None:
    out = tmp_path / "probe-repo.tar"
    proc = subprocess.run(
        [
            "/bin/bash",
            str(ROOT / "sandbox" / "fixtures" / "build-probe-tar.sh"),
            str(ROOT / "sandbox" / "fixtures" / "probe-repo"),
            str(out),
        ],
        capture_output=True,
        check=False,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr.decode()
    with tarfile.open(out) as archive:
        members = {m.name.removeprefix("./"): m for m in archive.getmembers()}
    assert members["docs/env"].issym() and members["docs/env"].linkname == "/proc/self/environ"
    assert members["pid1"].issym() and members["pid1"].linkname == "/proc/1/environ"
    assert members[".kiro/settings/mcp.json"].isreg()
    assert members[".kiro/hooks/canary.json"].isreg()
    assert members["AGENTS.md"].isreg()
    assert ".git/HEAD" in members  # snapshot.sh needs git ls-files over the fixture
    assert all(m.uid == 10001 for m in members.values())
