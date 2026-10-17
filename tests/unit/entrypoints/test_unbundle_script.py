"""The real sandbox/entrypoints/unbundle.sh (DESIGN §9.8, §9.13 step 4, R4-M1, R4-N1, R5-N1)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ossbot.common import gitcmd
from tests.githarness import UNBUNDLE_SH, GitWorld, run_script, tgit


def _stage(world: GitWorld, bundle: Path, name: str) -> None:
    data = bundle.read_bytes()  # the bundle may live in the output dir that is reset here
    for sub in ("input", "output"):
        d = world.io / sub
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    (world.io / "input" / name).write_bytes(data)


def _unbundle(world: GitWorld, mode: str, ref: str, sha: str, rev: int = 1) -> tuple[int, dict[str, object]]:
    proc = run_script(
        UNBUNDLE_SH, ["--mode", mode, "--bundle-ref", ref, "--checkout-sha", sha, "--rev", str(rev)], world
    )
    path = world.io / "output" / "result.json"
    assert path.exists(), proc.stderr.decode()
    return proc.returncode, json.loads(path.read_text())


def _operator_bundle(world: GitWorld, branch: str, base: str, extra_heads: tuple[str, ...] = ()) -> tuple[Path, str]:
    op = world.root / "operator"
    if op.exists():
        shutil.rmtree(op)
    tgit(world.root, "clone", "-q", str(world.upstream), str(op))
    tgit(op, "checkout", "-q", "-b", branch, base)
    world.write(op, {"src/feature.py": "x = 1\n"})
    world.commit(op, "operator commit 1")
    world.write(op, {"src/feature.py": "x = 2\n"})
    head = world.commit(op, "operator commit 2")
    bundle = world.root / "operator.bundle"
    tgit(op, "bundle", "create", "-q", str(bundle), f"refs/heads/{branch}", *extra_heads, f"^{base}")
    return bundle, head


def test_unbundle_script_real_bundle_fix_round(world: GitWorld) -> None:
    first = world.package(world.base, {"src/app.py": "print('fixed')\n"})
    repo = world.work / "repo"
    tgit(repo, "reset", "-q", "--soft", world.base)  # the runner discards the commit, keeps the tree
    world.write(repo, {"scratch.txt": "untracked leftovers\n"})
    _stage(world, first.out / "change.bundle", "change.bundle")
    code, result = _unbundle(world, "fix_round", gitcmd.CHANGE_REF, first.head, rev=1)
    assert code == 0, result
    assert result["ok"] is True
    assert tgit(repo, "rev-parse", "refs/ossbot/prev/r1") == first.head
    assert tgit(repo, "rev-parse", "HEAD") == first.head
    assert tgit(repo, "status", "--porcelain") == ""
    # the next round packages on top of the old head
    second = world.package(first.head, {"src/app.py": "print('fixed again')\n"})
    assert tgit(repo, "rev-parse", "HEAD") == first.head
    assert second.result["round_base"] == first.head


def test_unbundle_script_real_bundle_import(world: GitWorld) -> None:
    bundle, head = _operator_bundle(world, "feature-x", world.base)
    repo = world.sandbox_repo(world.base)
    _stage(world, bundle, "import.bundle")
    code, result = _unbundle(world, "import", "refs/heads/feature-x", world.base)
    assert code == 0, result
    assert tgit(repo, "rev-parse", "refs/ossbot/import") == head
    assert tgit(repo, "rev-parse", "HEAD") == world.base
    assert (repo / "src" / "feature.py").read_text() == "x = 2\n"
    assert "src/feature.py" in tgit(repo, "diff", "--cached", "--name-only")


@pytest.mark.parametrize("case", ["fix_round_with_import_head", "two_heads", "import_ref_mismatch"])
def test_unbundle_bundle_refs(world: GitWorld, case: str) -> None:
    world.sandbox_repo(world.base)
    if case == "two_heads":
        op = world.root / "operator"
        bundle, head = _operator_bundle(world, "feature-x", world.base)
        tgit(op, "branch", "other", head)
        tgit(op, "bundle", "create", "-q", str(bundle), "refs/heads/feature-x", "refs/heads/other", f"^{world.base}")
        _stage(world, bundle, "import.bundle")
        code, result = _unbundle(world, "import", "refs/heads/feature-x", world.base)
    elif case == "import_ref_mismatch":
        bundle, _ = _operator_bundle(world, "feature-x", world.base)
        _stage(world, bundle, "import.bundle")
        code, result = _unbundle(world, "import", "refs/heads/feature-y", world.base)
    else:
        bundle, head = _operator_bundle(world, "feature-x", world.base)
        _stage(world, bundle, "change.bundle")
        code, result = _unbundle(world, "fix_round", gitcmd.CHANGE_REF, head)
    assert code != 0
    assert result["error_class"] == "bundle_refs"


def test_unbundle_prereq_missing(world: GitWorld) -> None:
    op = world.root / "operator"
    tgit(world.root, "clone", "-q", str(world.upstream), str(op))
    tgit(op, "checkout", "-q", "-b", "ossbot-change", world.base)
    world.write(op, {"a.txt": "not upstream\n"})
    hidden = world.commit(op, "commit the sandbox never saw")
    world.write(op, {"a.txt": "head\n"})
    head = world.commit(op, "head")
    bundle = world.root / "prereq.bundle"
    tgit(op, "bundle", "create", "-q", str(bundle), gitcmd.CHANGE_REF, f"^{hidden}")
    world.sandbox_repo(world.base)
    _stage(world, bundle, "change.bundle")
    code, result = _unbundle(world, "fix_round", gitcmd.CHANGE_REF, head)
    assert code != 0
    assert result["error_class"] == "bundle_prereq_missing"


def test_package_script_bundle_shape(world: GitWorld) -> None:
    """M1: exactly refs/heads/ossbot-change, one prerequisite (the round base), signed-off commit."""
    out = world.package(world.base, {"src/app.py": "print('x')\n"})
    heads = tgit(world.upstream, "bundle", "list-heads", str(out.out / "change.bundle"))
    assert heads == f"{out.head} {gitcmd.CHANGE_REF}"
    header = (out.out / "change.bundle").read_bytes().split(b"\n\n", 1)[0].decode()
    assert [line[1:41] for line in header.splitlines() if line.startswith("-")] == [world.base]
    diffstat = json.loads((out.out / "diffstat.json").read_text())
    assert diffstat == [{"added": 1, "deleted": 3, "path": "src/app.py"}]
    assert (out.out / "change.patch").read_bytes().startswith(b"From ")
