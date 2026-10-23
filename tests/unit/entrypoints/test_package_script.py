"""The real sandbox/entrypoints/package.sh (DESIGN §9.11 step 3, I6): the work tree is staged in place
against a fresh repository (c_D1CYR0EKGR: copying an npm node_modules into the 2 GiB /tmp failed with
rsync exit 11), and every failure leaves result.json with a named error class."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from ossbot.common import gitcmd
from tests.githarness import EMAIL, IDENTITY, NAME, PACKAGE_SH, PACKAGE_TS, GitWorld, run_script, tgit

pytestmark = pytest.mark.unit

EDITS: dict[str, str | None] = {
    "src/app.py": "print('fixed')\n",  # edit
    "old.txt": None,  # deletion
    "src/new.py": "x = 1\n",  # new file
    "vendor/keep.log": "kept, changed\n",  # a force-tracked ignored file, changed
    "debug.log": "ignored, untracked\n",
}
EXPECTED = {".gitignore", "README.md", "link", "src/app.py", "src/new.py", "tools/run.sh", "vendor/keep.log"}


def _base(world: GitWorld) -> str:
    """Upstream gets a .gitignore for node_modules and *.log, a script and a force-tracked ignored file."""
    world.write(world.upstream, {".gitignore": "node_modules/\n*.log\n", "old.txt": "bye\n",
                                 "tools/run.sh": "#!/bin/sh\necho run\n", "vendor/keep.log": "kept\n"})  # fmt: skip
    tgit(world.upstream, "add", "-f", "vendor/keep.log")
    return world.commit(world.upstream, "add ignore rules")


def _sandbox(world: GitWorld, base: str) -> Path:
    """``/work/repo`` after fetch and the coder: a big ignored dependency tree, an exec bit, a symlink."""
    repo = world.sandbox_repo(base)
    big = repo / "node_modules" / "x" / "big.bin"
    big.parent.mkdir(parents=True)
    big.write_bytes(os.urandom(1 << 20) * 4)  # 4 MiB the old copy would have put into /tmp
    (repo / "tools" / "run.sh").chmod(0o755)
    (repo / "link").symlink_to("README.md")
    return repo


def _fetch_head(world: GitWorld, bundle: Path) -> Path:
    """A scratch repo holding the base (from upstream) and the bundle's branch."""
    scratch = world.root / "scratch"
    if scratch.exists():
        shutil.rmtree(scratch)
    tgit(world.root, "init", "-q", "--bare", str(scratch))
    tgit(scratch, "fetch", "-q", str(world.upstream), "+refs/heads/main:refs/heads/main")
    tgit(scratch, "fetch", "-q", str(bundle), f"{gitcmd.CHANGE_REF}:refs/heads/change")
    return scratch


def _snapshot(repo: Path) -> dict[str, str]:
    """Every path under ``repo`` (its .git included) -> mode and content (or link target) digest."""
    out: dict[str, str] = {}
    for path in sorted(repo.rglob("*")):
        rel = path.relative_to(repo).as_posix()
        if path.is_symlink():
            out[rel] = "link:" + os.readlink(path)
        elif path.is_file():
            out[rel] = f"{path.stat().st_mode:o}:" + hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            out[rel] = "dir"
    return out


def _stage_io(world: GitWorld) -> None:
    for sub in ("input", "output"):
        d = world.io / sub
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    (world.io / "input" / "commit_message.txt").write_text("fix: x\n", encoding="utf-8")


def test_ignored_dependency_tree_is_not_copied_or_committed(world: GitWorld) -> None:
    base = _base(world)
    _sandbox(world, base)
    got = world.package(base, EDITS)
    assert got.result == {"ok": True, "error_class": None, "head_sha": got.head, "round_base": base}
    scratch = _fetch_head(world, got.out / "change.bundle")
    assert set(tgit(scratch, "ls-tree", "-r", "--name-only", "refs/heads/change").splitlines()) == EXPECTED
    assert tgit(scratch, "rev-parse", "refs/heads/change^") == base
    assert tgit(scratch, "log", "-1", "--format=%an <%ae>|%cn <%ce>", "refs/heads/change") == f"{IDENTITY}|{IDENTITY}"
    assert f"Signed-off-by: {NAME} <{EMAIL}>" in tgit(scratch, "log", "-1", "--format=%B", "refs/heads/change")
    listing = tgit(scratch, "ls-tree", "-r", "refs/heads/change", "tools/run.sh", "link").splitlines()
    modes = {line.split("\t", 1)[1]: line.split(" ", 1)[0] for line in listing}
    assert modes == {"tools/run.sh": "100755", "link": "120000"}
    assert [p.name for p in (world.root / "pkgroot" / "pkg").iterdir()] == [".git"]  # nothing was copied
    diffstat = json.loads((got.out / "diffstat.json").read_text())
    assert {d["path"] for d in diffstat} >= {"src/app.py", "old.txt", "src/new.py", "vendor/keep.log", "link"}
    assert not [d for d in diffstat if str(d["path"]).startswith("node_modules") or d["path"] == "debug.log"]
    assert "node_modules" not in (got.out / "change.patch").read_text()


def test_work_tree_and_its_git_dir_are_untouched(world: GitWorld) -> None:
    base = _base(world)
    repo = _sandbox(world, base)
    world.write(repo, EDITS)
    head_before = tgit(repo, "rev-parse", "HEAD")
    status_before = tgit(repo, "status", "--porcelain", "--ignored")
    # Hostile repo-controlled git config, hooks and filters: none of them may run (I6, fresh repository).
    marker = world.root / "marker"
    hooks = world.root / "evil-hooks"
    hooks.mkdir()
    for hook in (hooks / "pre-commit", repo / ".git" / "hooks" / "pre-commit", repo / ".git" / "hooks" / "post-commit"):
        hook.write_text(f"#!/bin/sh\necho {hook.name} >> {marker}\n", encoding="utf-8")
        hook.chmod(0o755)
    config = repo / ".git" / "config"
    clean_config = config.read_text(encoding="utf-8")
    config.write_text(clean_config + f'[core]\n\thooksPath = {hooks}\n\tfsmonitor = "echo fsmonitor >> {marker}"\n'
                      f'[filter "evil"]\n\tclean = "echo filter >> {marker}; cat"\n', encoding="utf-8")  # fmt: skip
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("* filter=evil\n", encoding="utf-8")
    (repo / ".git" / "info" / "exclude").write_text("src/new.py\n", encoding="utf-8")  # never honoured
    before = _snapshot(repo)
    got = world.package(base, {})
    assert not marker.exists()
    assert _snapshot(repo) == before
    scratch = _fetch_head(world, got.out / "change.bundle")
    assert "src/new.py" in tgit(scratch, "ls-tree", "-r", "--name-only", "refs/heads/change").splitlines()
    config.write_text(clean_config, encoding="utf-8")  # back to a safe config before git runs in the repo
    (repo / ".git" / "info" / "attributes").unlink()
    (repo / ".git" / "info" / "exclude").unlink()
    assert tgit(repo, "rev-parse", "HEAD") == head_before
    assert tgit(repo, "status", "--porcelain", "--ignored") == status_before


def test_tree_equals_the_copy_semantics_and_is_deterministic(world: GitWorld) -> None:
    base = _base(world)
    repo = _sandbox(world, base)
    first = world.package(base, EDITS)
    # The old procedure, independently of the script: copy /work/repo minus its .git over a checkout of the
    # base (rsync --delete), then add -A.
    ref = world.root / "reference"
    tgit(world.root, "clone", "-q", str(world.upstream), str(ref))
    tgit(ref, "checkout", "-q", "--detach", base)
    for entry in ref.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for entry in repo.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.copytree(entry, ref / entry.name, symlinks=True)
        else:
            shutil.copy2(entry, ref / entry.name, follow_symlinks=False)
    tgit(ref, "add", "-A")
    want = tgit(ref, "write-tree")
    scratch = _fetch_head(world, first.out / "change.bundle")
    assert tgit(scratch, "rev-parse", "refs/heads/change^{tree}") == want
    again = world.package(base, {})  # a retry of the same rev (same tree) keeps the SHA
    assert again.head == first.head


@pytest.mark.parametrize("case", ["empty_change", "diffstat_failed", "base_missing", "package_failed"])
def test_failures_write_a_named_error_class(world: GitWorld, case: str) -> None:
    base = _base(world)
    round_base = base
    if case == "empty_change":  # only ignored and untracked-ignored files differ from the base
        repo = world.sandbox_repo(base)
        world.write(repo, {"node_modules/x/a.js": "1\n", "debug.log": "x\n"})
    else:
        world.write(_sandbox(world, base), EDITS)
    if case == "diffstat_failed":
        wrapper = world.bin / "numstat2json"
        wrapper.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    elif case == "base_missing":
        round_base = "f" * 40
    elif case == "package_failed":  # a step without its own fail(): the ERR trap still names it
        (world.root / "pkgroot").write_text("not a directory\n", encoding="utf-8")
    _stage_io(world)
    proc = run_script(PACKAGE_SH, ["--round-base", round_base, "--package-ts", PACKAGE_TS, "--name", NAME,
                                   "--email", EMAIL], world)  # fmt: skip
    path = world.io / "output" / "result.json"
    assert path.exists(), proc.stderr.decode()
    result = json.loads(path.read_text())
    assert proc.returncode == 3, proc.stderr.decode()
    assert result == {"ok": False, "error_class": case, "head_sha": None, "round_base": round_base}
