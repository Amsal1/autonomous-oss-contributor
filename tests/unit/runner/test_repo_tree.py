"""The scout's repository tree (DESIGN §9.5a rule 6; ADR-0011 addendum "scout repo read"):
``sandbox/entrypoints/lib/repo_tree.sh`` turns ``git ls-files`` into a depth-2 layout with per-directory
counts, deterministic and capped on whole lines; fetch.sh calls it and the full image ships it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "sandbox" / "entrypoints" / "lib" / "repo_tree.sh"
FETCH_SH = (ROOT / "sandbox" / "entrypoints" / "fetch.sh").read_text(encoding="utf-8")
FULL = (ROOT / "sandbox" / "images" / "full" / "Dockerfile").read_text(encoding="utf-8")
GIT_ENV = {
    "PATH": "/usr/bin:/bin",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["/usr/bin/git", *args], cwd=cwd, env={**GIT_ENV, "HOME": str(cwd)}, check=True,
                   capture_output=True)  # fmt: skip


def _tree(repo: Path, out: Path, *extra: str) -> str:
    proc = subprocess.run(["/bin/bash", str(SCRIPT), str(repo), str(out), *extra],
                          env={"PATH": "/usr/bin:/bin", "HOME": str(repo.parent)}, capture_output=True,
                          check=False)  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    return out.read_text(encoding="utf-8")


def _files(repo: Path, names: list[str]) -> None:
    for name in names:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _files(repo, ["README.md", "go.mod", "cmd/main.go", "pkg/a/a.go", "pkg/a/a_test.go", "pkg/b/c/d.go",
                  "pkg/util.go", "-rf", "docs/x\ny.md"])  # fmt: skip
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    (repo / "untracked.txt").write_text("not listed\n")
    return repo


def test_depth_two_layout_with_counts(repo: Path, tmp_path: Path) -> None:
    out = _tree(repo, tmp_path / "repo-tree.txt")
    assert out.splitlines() == [
        "# git ls-files of the default branch, depth 2, 9 files (generated; paths are repository data)",
        "-rf",
        "README.md",
        "cmd/ (1 file)",
        "cmd/main.go",
        "docs/ (1 file)",
        "docs/x\\ny.md",
        "go.mod",
        "pkg/ (4 files)",
        "pkg/a/ (2 files)",
        "pkg/b/ (1 file)",
        "pkg/util.go",
    ]
    assert "untracked" not in out  # tracked files only
    assert not list(tmp_path.glob("repo-tree.txt.*"))  # temp files removed


def test_output_is_deterministic(repo: Path, tmp_path: Path) -> None:
    assert _tree(repo, tmp_path / "a.txt").encode() == _tree(repo, tmp_path / "b.txt").encode()


def test_cap_on_whole_lines_with_a_truncation_line(tmp_path: Path) -> None:
    repo = tmp_path / "big"
    repo.mkdir()
    _files(repo, [f"dir{i:03d}/file{j}.txt" for i in range(60) for j in range(2)])
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    out = _tree(repo, tmp_path / "t.txt", "600")
    assert len(out.encode()) <= 600
    lines = out.splitlines()
    assert lines[0].startswith("# git ls-files of the default branch, depth 2, 120 files")
    assert lines[-1].startswith("# truncated: ") and lines[-1].endswith("list_directory (depth 1)")
    shown = len(lines) - 2
    assert lines[-1] == f"# truncated: {180 - shown} more entries; list a subdirectory with list_directory (depth 1)"
    assert all(ln.startswith("dir") for ln in lines[1:-1])  # whole entries only, no cut line
    full = _tree(repo, tmp_path / "full.txt")
    assert lines[1:-1] == full.splitlines()[1 : 1 + shown]


def test_fetch_sh_calls_it_after_the_head_and_the_image_ships_it() -> None:
    after = FETCH_SH.split("HEAD_SHA=", 1)[1]
    call = 'bash "$(dirname -- "$0")/lib/repo_tree.sh" "$REPO" "$OUT/repo-tree.txt" || rm -f -- "$OUT/repo-tree.txt"'
    assert call in after and after.index(call) < after.index("jq -n")
    assert "sandbox/entrypoints/lib/repo_tree.sh /opt/ossbot/entrypoints/lib/" in FULL
    script = SCRIPT.read_text(encoding="utf-8")
    assert '"${G[@]}" ls-files >"$LIST"' in script and "core.hooksPath=/dev/null" in script
    assert "-c core.quotePath" not in script  # left at its default: control characters stay C-quoted
