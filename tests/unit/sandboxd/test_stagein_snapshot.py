"""Stage-in tar, output extraction and the real snapshot.sh / probe.sh post-check (DESIGN §10.2, M3, H1)."""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from ossbot.sandboxd.snapshot import postcheck_clean
from ossbot.sandboxd.stagein import StageError, build_stagein_tar, extract_outputs

ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT_SH = ROOT / "sandbox" / "entrypoints" / "snapshot.sh"
PROBE_SH = ROOT / "sandbox" / "entrypoints" / "probe.sh"


# ---------------------------------------------------------------------------- stage-in
def test_stagein_tar_regular_0444_safe_names(tmp_path: Path) -> None:
    root = tmp_path / "runner"
    root.mkdir()
    (root / "plan.md").write_text("plan")
    (root / "issue.json").write_text("{}")
    tar = build_stagein_tar({"plan.md": root / "plan.md", "issue.json": root / "issue.json"}, [root])
    with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
        members = archive.getmembers()
        assert sorted(m.name for m in members) == ["issue.json", "plan.md"]
        for m in members:
            assert m.isreg() and m.mode == 0o444 and m.uid == 0 and m.gid == 0 and m.mtime == 0
        handle = archive.extractfile("plan.md")
        assert handle is not None and handle.read() == b"plan"


@pytest.mark.parametrize("name", ["../x", "a/b", "Upper.md", "", "x" * 65, ".", ".."])
def test_stagein_rejects_bad_names(name: str, tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x")
    with pytest.raises(StageError):
        build_stagein_tar({name: tmp_path / "f"}, [tmp_path])


def test_stagein_rejects_symlink_fifo_dir_and_outside(tmp_path: Path) -> None:
    root = tmp_path / "runner"
    root.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("s")
    (root / "link").symlink_to(secret)
    os.mkfifo(root / "fifo")
    (root / "dir").mkdir()
    for name in ("link", "fifo", "dir"):
        with pytest.raises(StageError):
            build_stagein_tar({"x": root / name}, [root])
    with pytest.raises(StageError):
        build_stagein_tar({"x": secret}, [root])
    with pytest.raises(StageError):
        build_stagein_tar({"x": root / ".." / "secret"}, [root])


def _tar(*members: tuple[str, bytes | None, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as archive:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
            elif kind == "link":
                info.type, info.linkname = tarfile.SYMTYPE, "/proc/self/environ"
            elif kind == "fifo":
                info.type = tarfile.FIFOTYPE
            else:
                info.size = len(data or b"")
            archive.addfile(info, io.BytesIO(data or b"") if kind == "file" else None)
    return buf.getvalue()


def test_extract_outputs_regular_files(tmp_path: Path) -> None:
    data = _tar(
        ("output", None, "dir"),
        ("output/result.json", b"{}", "file"),
        ("output/logs", None, "dir"),
        ("output/logs/unit.log", b"ok", "file"),
    )
    written = extract_outputs([data], tmp_path / "dest")
    assert sorted(p.relative_to(tmp_path / "dest").as_posix() for p in written) == ["logs/unit.log", "result.json"]
    assert oct((tmp_path / "dest" / "result.json").stat().st_mode & 0o777) == "0o640"


@pytest.mark.parametrize(
    "member",
    [
        ("output/env", None, "link"),
        ("output/f", None, "fifo"),
        ("output/../x", b"x", "file"),
        ("/etc/x", b"x", "file"),
        ("other/x", b"x", "file"),
    ],
)
def test_extract_outputs_rejects_hostile_members(member: tuple[str, bytes | None, str], tmp_path: Path) -> None:
    with pytest.raises(StageError):
        extract_outputs([_tar(("output", None, "dir"), member)], tmp_path / "dest")


# ---------------------------------------------------------------------------- snapshot
def _git(cwd: Path, *args: str) -> None:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(cwd),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    subprocess.run(["/usr/bin/git", *args], cwd=cwd, env=env, check=True, capture_output=True)


def _run(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["/bin/bash", str(script)], env={"PATH": "/usr/bin:/bin", **env}, capture_output=True, check=False
    )


@pytest.fixture
def hostile_repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    repo = work / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "README.md").write_text("readme\n")
    (repo / "src" / "app.py").write_text("print('x')\n")
    (repo / ".gitignore").write_text("ignored.txt\n")
    os.symlink("/proc/self/environ", repo / "docs" / "env")
    os.symlink("/proc/1/environ", repo / "pid1")
    (repo / "big.bin").write_bytes(b"\0" * (6 * 1024 * 1024))
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "core.symlinks=true", "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    # after the commit: hostile changes a validate job could make on the task volume
    (repo / "new_untracked.py").write_text("x = 1\n")
    (repo / "ignored.txt").write_text("ignored\n")
    os.mkfifo(repo / "pipe")
    os.symlink("/etc", repo / "etclink")
    return work


def test_snapshot_script_produces_only_regular_files_and_stubs(hostile_repo: Path, tmp_path: Path) -> None:
    snap = tmp_path / "snap"
    snap.mkdir()
    proc = _run(
        SNAPSHOT_SH,
        {"OSSBOT_WORK": str(hostile_repo), "OSSBOT_SNAP": str(snap), "OSSBOT_SNAPSHOT_HOME": str(tmp_path / "h")},
    )
    assert proc.returncode == 0, proc.stderr.decode()
    entries = {p.relative_to(snap).as_posix(): p for p in snap.rglob("*")}
    assert all(p.is_file() or p.is_dir() for p in entries.values())
    assert not any(p.is_symlink() for p in entries.values())
    assert (snap / "docs" / "env.symlink.txt").read_text() == "SYMLINK -> /proc/self/environ\n"
    assert (snap / "pid1.symlink.txt").read_text() == "SYMLINK -> /proc/1/environ\n"
    assert "docs/env" not in entries and "pid1" not in entries
    assert "big.bin" not in entries  # > 5 MB
    assert "new_untracked.py" in entries and "ignored.txt" not in entries
    assert "pipe" not in entries and "etclink" not in entries
    assert not any(k == ".git" or k.startswith(".git/") for k in entries)
    assert (snap / "src" / "app.py").read_text() == "print('x')\n"


def _postcheck(repo_dir: Path) -> bytes:
    work = repo_dir.parent
    proc = _run(PROBE_SH, {"OSSBOT_PROBE": "snapshot_check", "OSSBOT_WORK": str(work)})
    assert proc.returncode == 0, proc.stderr.decode()
    return proc.stdout


def test_postcheck_clean_snapshot(tmp_path: Path) -> None:
    repo = tmp_path / "w" / "repo"
    (repo / "d").mkdir(parents=True)
    (repo / "d" / "f").write_text("x")
    assert postcheck_clean(_postcheck(repo))


@pytest.mark.parametrize("plant", ["symlink", "fifo"])
def test_postcheck_rejects_planted_entry(plant: str, tmp_path: Path) -> None:
    repo = tmp_path / "w" / "repo"
    repo.mkdir(parents=True)
    (repo / "ok.txt").write_text("x")
    if plant == "symlink":
        os.symlink("/proc/self/environ", repo / "env")
    else:
        os.mkfifo(repo / "p")
    output = _postcheck(repo)
    assert not postcheck_clean(output)
    assert output.strip().endswith(b"env" if plant == "symlink" else b"p")
