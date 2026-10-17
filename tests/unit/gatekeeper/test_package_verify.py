"""/package verification on local bare repos with the production GIT_ENV/GIT_ARGS/BUNDLE_FETCH_ARGS
(DESIGN §12.5 steps 1-8, §12.5a, §22.1, M1, M7, M9, M12, M13, R4-M1, R4-N6, R5-N1, AC 15)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from ossbot.common import gitcmd
from ossbot.common.hashing import sha256_hex, sha256_text
from ossbot.common.paths import Paths
from ossbot.common.schemas import State
from ossbot.gatekeeper import db, derive, gitops, live_checks
from ossbot.gatekeeper.approvals import Superseded
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.gitops import BUNDLE_FETCH_ARGS, GIT_ARGS, GIT_ENV, GitError
from ossbot.gatekeeper.packaging import gitleaks_scan, prune_retention
from tests.conftest import UID_WORKER, insert_contribution
from tests.githarness import (
    IDENTITY,
    NUMSTAT2JSON,
    PACKAGE_TS,
    GitWorld,
    PackageOut,
    package_request,
    stage_artifacts,
    tgit,
)
from tests.unit.gatekeeper.api_helpers import client

VETTED = json.dumps({"ai_policy": "allowed"})


def packaging(ctx: AppContext, **cols: Any) -> str:
    return insert_contribution(ctx, State.PACKAGING, vetting_json=cols.pop("vetting_json", VETTED), **cols)


async def post(ctx: AppContext, cid: str, body: dict[str, Any]) -> httpx.Response:
    async with client(ctx, UID_WORKER) as c:
        return await c.post(f"/v1/contributions/{cid}/package", json=body)


async def package_ok(
    ctx: AppContext, world: GitWorld, cid: str, out: PackageOut, rev: int = 1, **kw: Any
) -> dict[str, Any]:
    sums = stage_artifacts(ctx.paths, cid, rev, out.out)
    resp = await post(ctx, cid, package_request(ctx.paths, cid, rev, sums, out.head, kw.pop("base", world.base), **kw))
    assert resp.status_code == 200, resp.text
    doc: dict[str, Any] = resp.json()
    return doc


def outputs_from(
    world: GitWorld, repo: Path, base: str, out: Path, refs: tuple[str, ...] = (gitcmd.CHANGE_REF,)
) -> str:
    """Build sandbox-shaped outputs (bundle, fixed patch, diffstat) from a hand-made repo."""
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    head = tgit(repo, "rev-parse", refs[0])
    tgit(repo, "bundle", "create", "-q", str(out / "change.bundle"), *refs, f"^{base}")
    patch = tgit(repo, *gitcmd.FORMAT_PATCH_ARGV, f"{base}..{head}")
    (out / "change.patch").write_text(patch + "\n", encoding="utf-8")
    numstat = tgit(repo, *gitcmd.NUMSTAT_ARGV, base, head)
    js = subprocess.run(
        [sys.executable, str(NUMSTAT2JSON)], input=numstat.encode() + b"\n", capture_output=True, check=True
    )
    (out / "diffstat.json").write_bytes(js.stdout)
    return head


def hand_repo(world: GitWorld, name: str = "hand") -> Path:
    repo = world.root / name
    if repo.exists():
        shutil.rmtree(repo)
    tgit(world.root, "clone", "-q", str(world.upstream), str(repo))
    tgit(repo, "checkout", "-q", "-B", "ossbot-change", world.base)
    return repo


def commit_as(world: GitWorld, repo: Path, files: dict[str, str], *, author: str = IDENTITY, committer: str = IDENTITY,
              signoff: str = IDENTITY) -> str:  # fmt: skip
    world.write(repo, files)
    a_name, a_mail = author[:-1].split(" <")
    c_name, c_mail = committer[:-1].split(" <")
    env = {"GIT_AUTHOR_NAME": a_name, "GIT_AUTHOR_EMAIL": a_mail, "GIT_COMMITTER_NAME": c_name,
           "GIT_COMMITTER_EMAIL": c_mail, "GIT_AUTHOR_DATE": PACKAGE_TS, "GIT_COMMITTER_DATE": PACKAGE_TS}  # fmt: skip
    tgit(repo, "add", "-A")
    tgit(repo, "commit", "-q", "-m", f"fix: hand made\n\nSigned-off-by: {signoff}", env=env)
    return tgit(repo, "rev-parse", "HEAD")


def state_of(ctx: AppContext, cid: str) -> str:
    row = db.one(ctx.conn, "SELECT state FROM contributions WHERE id = ?", (cid,))
    assert row is not None
    return str(row["state"])


# ------------------------------------------------------------------------------ M1 / R4-M1
async def test_real_package_script_bundle(gctx: AppContext, world: GitWorld) -> None:
    cid = packaging(gctx)
    first = world.package(world.base, {"src/app.py": "print('fixed')\n"})
    doc = await package_ok(gctx, world, cid, first)
    assert doc["kind"] == "open_pr"
    assert doc["head_sha"] == first.head
    row = db.one(gctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
    assert row is not None
    assert row["state"] == "AWAITING_APPROVAL"
    assert row["upstream_base_tip"] == world.base
    assert row["bundle_sha256"] == sha256_hex((first.out / "change.bundle").read_bytes())
    assert "Signed-off-by: Test Person <test.person@example.org>" in row["commit_message"]
    approval = db.one(gctx.conn, "SELECT * FROM approvals WHERE id = ?", (doc["approval_id"],))
    assert approval is not None
    binding = json.loads(approval["binding_json"])
    derived = gctx.gitops.derived_dir(cid, 1)
    assert binding["patch_sha256"] == sha256_hex((derived / "change.patch").read_bytes())
    assert binding["head_sha"] == first.head
    assert binding["title_sha256"] == sha256_text("fix(web): handle empty spans")

    # fix round on the open PR: the gatekeeper picks push_update because pr_number is set (M9)
    await fix_round_ready(gctx, world, cid, first, doc["action_id"])
    second = world.package(first.head, {"src/app.py": "print('fixed twice')\n"})
    doc2 = await package_ok(gctx, world, cid, second, rev=2, title=None, body=None)
    assert doc2["kind"] == "push_update"
    payload = json.loads(db.one(gctx.conn, "SELECT payload_json FROM outbound_actions WHERE id = ?",
                                (doc2["action_id"],))["payload_json"])  # fmt: skip
    assert payload["old_head_sha"] == first.head
    assert payload["pr_number"] == 41
    meta = json.loads((gctx.gitops.derived_dir(cid, 2) / "meta.json").read_text())
    assert meta["round_base"] == first.head
    assert meta["cumulative_numstat"].strip().endswith("src/app.py")


async def fix_round_ready(gctx: AppContext, world: GitWorld, cid: str, first: PackageOut, action_id: str) -> None:
    """Play the earlier submit of rev 1 and the fix-round unbundle in the sandbox."""
    from tests.unit.entrypoints.test_unbundle_script import _stage, _unbundle

    branch = json.loads(db.one(gctx.conn, "SELECT payload_json FROM outbound_actions WHERE id = ?",
                               (action_id,))["payload_json"])["branch"]  # fmt: skip
    bundle_copy = world.root / "r1.bundle"
    shutil.copy(first.out / "change.bundle", bundle_copy)
    world.push_fork(bundle_copy, first.head, branch)
    with db.transaction(gctx.conn):
        gctx.conn.execute(
            "UPDATE outbound_actions SET status = 'done', executed_at = ?, result_json = ? WHERE id = ?",
            ("2026-10-17T12:00:00.000000Z", json.dumps({"fork": "test-bot-login/langfuse"}), action_id),
        )
        gctx.conn.execute(
            "UPDATE contributions SET state = 'PACKAGING', pr_number = 41, head_sha = ?, branch = ? WHERE id = ?",
            (first.head, branch, cid),
        )
    repo = world.work / "repo"
    tgit(repo, "reset", "-q", "--soft", world.base)
    _stage(world, bundle_copy, "change.bundle")
    code, result = _unbundle(world, "fix_round", gitcmd.CHANGE_REF, first.head)
    assert code == 0, result


async def test_bundle_fetch_needs_scoped_override(gctx: AppContext, world: GitWorld) -> None:
    out = world.package(world.base, {"src/app.py": "print('x')\n"})
    cid = packaging(gctx)
    stage_artifacts(gctx.paths, cid, 1, out.out)
    ops = gctx.gitops
    tip = ops.fetch_upstream("langfuse/langfuse", "main")
    mirror = ops.create_mirror("langfuse/langfuse", cid, 1, "main", tip)
    copy = ops.copy_bundle(cid, 1)
    with pytest.raises(GitError) as err:
        ops.git.run(["fetch", "-q", "--no-tags", str(copy.path), f"{gitcmd.CHANGE_REF}:refs/x"], cwd=mirror,
                    git_args=GIT_ARGS)  # fmt: skip
    assert "transport 'file' not allowed" in err.value.stderr
    ops.fetch_bundle(mirror, copy.path, gitcmd.CHANGE_REF, "refs/ossbot/r1")
    assert ops.rev_parse(mirror, "refs/ossbot/r1") == out.head
    assert BUNDLE_FETCH_ARGS[: len(GIT_ARGS)] == GIT_ARGS
    assert gitops.effective_config(BUNDLE_FETCH_ARGS, "protocol.file.allow") == "always"
    assert GIT_ENV["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert GIT_ENV["GIT_CONFIG_NOSYSTEM"] == "1"


async def test_numstat_pinned(gctx: AppContext, world: GitWorld) -> None:
    """R4-N6: a rename and a copy give the same (added, deleted, path) set on both sides."""
    world.write(world.upstream, {"big.txt": "".join(f"line {i}\n" for i in range(40))})
    base = world.commit(world.upstream, "add big file")
    world.sandbox_repo(base)
    repo = world.work / "repo"
    content = (repo / "big.txt").read_text()
    out = world.package(base, {"big.txt": None, "moved/big.txt": content, "copy.txt": content}, repo_setup=False)
    cid = packaging(gctx)
    sums = stage_artifacts(gctx.paths, cid, 1, out.out)
    resp = await post(gctx, cid, package_request(gctx.paths, cid, 1, sums, out.head, base))
    assert resp.status_code == 200, resp.text
    sandbox = derive.parse_diffstat_json((out.out / "diffstat.json").read_bytes())
    mirror = gctx.gitops.mirror_dir("langfuse/langfuse", cid, 1)
    derived = derive.derive(gctx.gitops, mirror, base, out.head)
    assert sandbox == set(derived.numstat)
    assert {e.path for e in derived.numstat} == {"big.txt", "copy.txt", "moved/big.txt"}


@pytest.mark.parametrize("case", ["detached_head", "two_heads", "other_head"])
async def test_bundle_refs_rejected(gctx: AppContext, world: GitWorld, case: str) -> None:
    repo = hand_repo(world)
    head = commit_as(world, repo, {"src/app.py": "print('y')\n"})
    out = world.root / "hand-out"
    if case == "detached_head":
        tgit(repo, "checkout", "-q", "--detach", head)
        outputs_from(world, repo, world.base, out, refs=("HEAD",))
    elif case == "two_heads":
        tgit(repo, "branch", "-f", "extra", head)
        outputs_from(world, repo, world.base, out, refs=(gitcmd.CHANGE_REF, "refs/heads/extra"))
    else:
        tgit(repo, "branch", "-f", "feature", head)
        outputs_from(world, repo, world.base, out, refs=("refs/heads/feature",))
    cid = packaging(gctx)
    sums = stage_artifacts(gctx.paths, cid, 1, out)
    resp = await post(gctx, cid, package_request(gctx.paths, cid, 1, sums, head, world.base))
    assert resp.status_code == 422
    assert resp.json()["error"] == "bundle_refs"
    assert state_of(gctx, cid) == "PACKAGING"


# ---------------------------------------------------------------------------- M7 compare
async def test_patch_compare_tolerates_git_version(gctx: AppContext, world: GitWorld) -> None:
    out = world.package(world.base, {"src/app.py": "print('v')\n"})
    patch = out.out / "change.patch"
    patch.write_bytes(patch.read_bytes() + b"-- \n2.99.1\n\n\n")  # another git's signature + trailing bytes
    cid = packaging(gctx)
    await package_ok(gctx, world, cid, out)
    assert gctx.control.view().state == "running"
    assert state_of(gctx, cid) == "AWAITING_APPROVAL"


@pytest.mark.parametrize("what", ["patch_content", "diffstat"])
async def test_patch_mismatch_rejected(gctx: AppContext, world: GitWorld, what: str) -> None:
    out = world.package(world.base, {"src/app.py": "print('v')\n"})
    if what == "patch_content":
        patch = out.out / "change.patch"
        patch.write_bytes(patch.read_bytes().replace(b"+print('v')", b"+print('w')"))
    else:
        (out.out / "diffstat.json").write_text(json.dumps([{"added": 9, "deleted": 0, "path": "src/app.py"}]))
    cid = packaging(gctx)
    sums = stage_artifacts(gctx.paths, cid, 1, out.out)
    resp = await post(gctx, cid, package_request(gctx.paths, cid, 1, sums, out.head, world.base))
    assert resp.status_code == 422
    assert resp.json()["error"] == "patch_mismatch"
    assert state_of(gctx, cid) == "FAILED"
    assert gctx.control.view().state == "paused"  # anomaly
    assert gctx.paths.paused_flag.exists()


async def test_patch_unreadable_rejected(gctx: AppContext, world: GitWorld) -> None:
    out = world.package(world.base, {"src/app.py": "print('v')\n"})
    (out.out / "diffstat.json").unlink()
    cid = packaging(gctx)
    sums = stage_artifacts(gctx.paths, cid, 1, out.out)
    resp = await post(gctx, cid, package_request(gctx.paths, cid, 1, sums, out.head, world.base))
    assert resp.status_code == 422
    assert resp.json()["error"] == "patch_unreadable"
    assert state_of(gctx, cid) == "FAILED"
    assert gctx.control.view().state == "paused"


# --------------------------------------------------------------------------- history rules
async def test_fabricated_base_rejected(gctx: AppContext, world: GitWorld) -> None:
    repo = hand_repo(world)
    fake_base = commit_as(world, repo, {"src/app.py": "print('fabricated base')\n"})
    head = commit_as(world, repo, {"src/app.py": "print('on top')\n"})
    out = world.root / "fab-out"
    outputs_from(world, repo, world.base, out)  # the bundle carries the fabricated base itself
    cid = packaging(gctx)
    sums = stage_artifacts(gctx.paths, cid, 1, out)
    resp = await post(gctx, cid, package_request(gctx.paths, cid, 1, sums, head, fake_base))
    assert resp.status_code == 422
    assert resp.json()["error"] == "fabricated_base"


async def test_multi_commit_rejected(gctx: AppContext, world: GitWorld) -> None:
    repo = hand_repo(world)
    commit_as(world, repo, {"src/app.py": "print(1)\n"})
    head = commit_as(world, repo, {"src/app.py": "print(2)\n"})
    out = world.root / "multi-out"
    outputs_from(world, repo, world.base, out)
    cid = packaging(gctx)
    sums = stage_artifacts(gctx.paths, cid, 1, out)
    resp = await post(gctx, cid, package_request(gctx.paths, cid, 1, sums, head, world.base))
    assert resp.status_code == 422
    assert resp.json()["error"] == "multi_commit"


async def test_parent_not_base_rejected(gctx: AppContext, world: GitWorld) -> None:
    newer = world.advance_upstream()
    out = world.package(world.base, {"src/app.py": "print('old base')\n"})
    cid = packaging(gctx)
    sums = stage_artifacts(gctx.paths, cid, 1, out.out)
    resp = await post(gctx, cid, package_request(gctx.paths, cid, 1, sums, out.head, newer))
    assert resp.status_code == 422
    assert resp.json()["error"] == "parent_not_base"


@pytest.mark.parametrize("who", ["author", "committer", "signoff"])
async def test_identity_mismatch_rejected(gctx: AppContext, world: GitWorld, who: str) -> None:
    other = "Someone Else <else@example.org>"
    repo = hand_repo(world)
    head = commit_as(world, repo, {"src/app.py": "print('id')\n"}, **{who: other})
    out = world.root / "id-out"
    outputs_from(world, repo, world.base, out)
    cid = packaging(gctx)
    sums = stage_artifacts(gctx.paths, cid, 1, out)
    resp = await post(gctx, cid, package_request(gctx.paths, cid, 1, sums, head, world.base))
    assert resp.status_code == 422
    assert resp.json() == {"error": "identity_mismatch", "detail": who}


async def test_claimed_head_mismatch(gctx: AppContext, world: GitWorld) -> None:
    out = world.package(world.base, {"src/app.py": "print('c')\n"})
    cid = packaging(gctx)
    sums = stage_artifacts(gctx.paths, cid, 1, out.out)
    resp = await post(gctx, cid, package_request(gctx.paths, cid, 1, sums, "f" * 40, world.base))
    assert resp.json()["error"] == "claimed_head_mismatch"


@pytest.mark.parametrize("state", [State.REVIEW_PASSED, State.AWAITING_APPROVAL, State.IMPLEMENTING])
async def test_package_only_in_packaging(gctx: AppContext, world: GitWorld, state: State) -> None:
    out = world.package(world.base, {"src/app.py": "print('s')\n"})
    cid = insert_contribution(gctx, state, vetting_json=VETTED)
    sums = stage_artifacts(gctx.paths, cid, 1, out.out)
    resp = await post(gctx, cid, package_request(gctx.paths, cid, 1, sums, out.head, world.base))
    assert resp.status_code == 409
    assert resp.json()["error"] == "not_packaging"


async def test_hostile_git_config_ignored(gctx: AppContext, world: GitWorld, tmp_path: Path) -> None:
    """Repo config with fsmonitor, hooks, a textconv/filter driver; attributes in the tree. Nothing runs."""
    canary = tmp_path / "canary"
    repo = world.sandbox_repo(world.base)
    evil = f"sh -c 'echo pwned >> {canary}'"
    for key, value in (
        ("core.fsmonitor", evil), ("diff.evil.textconv", evil), ("diff.evil.command", evil),
        ("filter.evil.clean", evil), ("filter.evil.smudge", evil), ("core.hooksPath", str(repo / "hooks")),
    ):  # fmt: skip
        tgit(repo, "config", key, value)
    (repo / "hooks").mkdir()
    hook = repo / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\necho hook >> {canary}\n")
    hook.chmod(0o755)
    out = world.package(world.base, {".gitattributes": "* diff=evil filter=evil\n", "src/app.py": "print('h')\n"},
                        repo_setup=False)  # fmt: skip
    cid = packaging(gctx)
    await package_ok(gctx, world, cid, out)
    assert not canary.exists()


# ------------------------------------------------------------------------------- push_update
async def _packaged_open_pr(gctx: AppContext, world: GitWorld) -> tuple[str, PackageOut, dict[str, Any]]:
    cid = packaging(gctx)
    first = world.package(world.base, {"src/app.py": "print('first')\n"})
    doc = await package_ok(gctx, world, cid, first)
    await fix_round_ready(gctx, world, cid, first, doc["action_id"])
    return cid, first, doc


async def test_push_update_happy(gctx: AppContext, world: GitWorld) -> None:
    cid, first, _ = await _packaged_open_pr(gctx, world)
    second = world.package(first.head, {"src/app.py": "print('second')\n"})
    doc = await package_ok(gctx, world, cid, second, rev=2, title=None, body=None)
    assert doc["kind"] == "push_update"
    assert state_of(gctx, cid) == "AWAITING_APPROVAL"


async def test_push_update_branch_moved(gctx: AppContext, world: GitWorld) -> None:
    cid, first, _ = await _packaged_open_pr(gctx, world)
    branch = db.one(gctx.conn, "SELECT branch FROM contributions WHERE id = ?", (cid,))["branch"]
    # someone else pushes to the fork branch
    other = hand_repo(world, "intruder")
    tgit(other, "fetch", "-q", str(world.fork), f"refs/heads/{branch}:refs/heads/theirs")
    tgit(other, "checkout", "-q", "theirs")
    moved = commit_as(world, other, {"src/app.py": "print('intruder')\n"})
    tgit(other, "push", "-q", str(world.fork), f"{moved}:refs/heads/{branch}")
    second = world.package(first.head, {"src/app.py": "print('second')\n"})
    sums = stage_artifacts(gctx.paths, cid, 2, second.out)
    resp = await post(gctx, cid, package_request(gctx.paths, cid, 2, sums, second.head, world.base, title=None,
                                                 body=None))  # fmt: skip
    assert resp.status_code == 422
    assert resp.json()["error"] == "branch_moved"
    row = db.one(gctx.conn, "SELECT state, state_reason, watch_rounds FROM contributions WHERE id = ?", (cid,))
    assert row["state"] == "WATCHING"
    assert row["state_reason"] == "fix_round_failed:branch_moved"
    assert row["watch_rounds"] == gctx.config.policy.limits.watch_rounds_max
    pending = gctx.conn.execute(
        "SELECT COUNT(*) FROM outbound_actions WHERE cid = ? AND kind = 'push_update'", (cid,)
    ).fetchone()[0]
    assert pending == 0


async def test_push_update_two_commits_rejected(gctx: AppContext, world: GitWorld) -> None:
    cid, first, _ = await _packaged_open_pr(gctx, world)
    repo = world.root / "two"
    tgit(world.root, "clone", "-q", str(world.fork), str(repo))
    branch = db.one(gctx.conn, "SELECT branch FROM contributions WHERE id = ?", (cid,))["branch"]
    tgit(repo, "checkout", "-q", "-B", "ossbot-change", f"origin/{branch}")
    commit_as(world, repo, {"src/app.py": "print('a')\n"})
    head = commit_as(world, repo, {"src/app.py": "print('b')\n"})
    out = world.root / "two-out"
    outputs_from(world, repo, first.head, out)
    sums = stage_artifacts(gctx.paths, cid, 2, out)
    resp = await post(gctx, cid, package_request(gctx.paths, cid, 2, sums, head, world.base, title=None, body=None))
    assert resp.status_code == 422
    assert resp.json()["error"] == "multi_commit"


async def test_package_kind_from_pr_number(gctx: AppContext) -> None:
    cid = insert_contribution(gctx, State.WATCHING, pr_number=5)
    async with client(gctx, UID_WORKER) as c:
        resp = await c.post("/v1/actions", json={"cid": cid, "kind": "push_update", "target_number": 5, "body": "x"})
        assert resp.status_code == 400
        resp = await c.post(f"/v1/contributions/{cid}/package", json={"kind": "open_pr"})
        assert resp.status_code == 400  # the request model has no kind field (extra=forbid)


# ---------------------------------------------------------------------------------- mirrors
async def test_mirror_alternates_no_foreign_refs(gctx: AppContext, world: GitWorld) -> None:
    cid1 = packaging(gctx)
    out1 = world.package(world.base, {"src/app.py": "print('one')\n"})
    await package_ok(gctx, world, cid1, out1)
    world.sandbox_repo(world.base)
    out2 = world.package(world.base, {"src/app.py": "print('two')\n"}, repo_setup=False)
    cid2 = packaging(gctx)
    await package_ok(gctx, world, cid2, out2)
    mirror1 = gctx.gitops.mirror_dir("langfuse/langfuse", cid1, 1)
    refs = tgit(mirror1, "for-each-ref", "--format=%(refname)").splitlines()
    assert sorted(refs) == ["refs/ossbot/r1", "refs/upstream/main"]
    reachable = tgit(mirror1, "rev-list", "--all").splitlines()
    assert out2.head not in reachable
    assert (mirror1 / "objects" / "info" / "alternates").read_text().strip().endswith("langfuse__langfuse.git/objects")
    cache_refs = tgit(gctx.gitops.cache_dir("langfuse/langfuse"), "for-each-ref", "--format=%(refname)").splitlines()
    assert cache_refs == ["refs/upstream/main"]


async def test_superseded_mirror_deleted(gctx: AppContext, world: GitWorld) -> None:
    cid = packaging(gctx)
    out1 = world.package(world.base, {"src/app.py": "print('one')\n"})
    doc1 = await package_ok(gctx, world, cid, out1)
    with db.transaction(gctx.conn):
        gctx.conn.execute("UPDATE contributions SET state = 'PACKAGING' WHERE id = ?", (cid,))
    out2 = world.package(world.base, {"src/app.py": "print('two')\n"}, repo_setup=False)
    await package_ok(gctx, world, cid, out2, rev=2)
    assert not gctx.gitops.mirror_dir("langfuse/langfuse", cid, 1).exists()
    assert not gctx.gitops.derived_dir(cid, 1).exists()
    assert not gctx.gitops.work_bundle(cid, 1).exists()
    assert gctx.gitops.mirror_dir("langfuse/langfuse", cid, 2).exists()
    old = db.one(gctx.conn, "SELECT status, invalidated_reason FROM approvals WHERE id = ?", (doc1["approval_id"],))
    assert (old["status"], old["invalidated_reason"]) == ("invalidated", "new_package_rev")
    action = db.one(gctx.conn, "SELECT status FROM outbound_actions WHERE id = ?", (doc1["action_id"],))
    assert action["status"] == "cancelled"
    # terminal contribution: housekeeping removes what is left
    assert prune_retention(gctx) == []
    with db.transaction(gctx.conn):
        gctx.conn.execute("UPDATE contributions SET state = 'REJECTED' WHERE id = ?", (cid,))
    assert prune_retention(gctx) == [(cid, 2)]
    assert not gctx.gitops.mirror_dir("langfuse/langfuse", cid, 2).exists()


# ------------------------------------------------------------------------------------ R5-N1
@pytest.mark.parametrize("attack", ["directory", "symlink", "symlinked_rev_dir", "oversize", "missing"])
async def test_artifact_bundle_hardening(
    gctx: AppContext, world: GitWorld, attack: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = world.package(world.base, {"src/app.py": "print('r5')\n"})
    cid = packaging(gctx)
    sums = stage_artifacts(gctx.paths, cid, 1, out.out)
    target = gctx.paths.artifacts_dir / cid / "1" / "change.bundle"
    if attack == "directory":  # git would fetch a directory as a local repository (R5-N1)
        target.unlink()
        tgit(gctx.paths.artifacts_dir / cid / "1", "init", "-q", str(target))
    elif attack == "symlink":
        real = world.root / "real.bundle"
        shutil.move(target, real)
        target.symlink_to(real)
    elif attack == "symlinked_rev_dir":
        moved = world.root / "revdir"
        shutil.move(gctx.paths.artifacts_dir / cid / "1", moved)
        (gctx.paths.artifacts_dir / cid / "1").symlink_to(moved)
    elif attack == "oversize":
        monkeypatch.setattr(gitops, "MAX_BUNDLE_BYTES", 64)
    else:
        target.unlink()
    resp = await post(gctx, cid, package_request(gctx.paths, cid, 1, sums, out.head, world.base))
    assert resp.status_code == 422
    assert resp.json()["error"] == "bundle_invalid"


async def test_bundle_commands_run_on_private_copy(gctx: AppContext, world: GitWorld) -> None:
    out = world.package(world.base, {"src/app.py": "print('copy')\n"})
    cid = packaging(gctx)
    await package_ok(gctx, world, cid, out)
    copy = gctx.gitops.work_bundle(cid, 1)
    assert copy.stat().st_mode & 0o777 == 0o600
    bundle_calls = [argv for argv in gctx.gitops.git.calls if any(a.endswith(".bundle") for a in argv)]
    assert bundle_calls
    for argv in bundle_calls:
        paths_in = [a for a in argv if a.endswith(".bundle")]
        assert paths_in == [str(copy)]
        assert str(gctx.paths.artifacts_dir) not in " ".join(argv)


async def test_bundle_sha256_must_match(gctx: AppContext, world: GitWorld) -> None:
    out = world.package(world.base, {"src/app.py": "print('sha')\n"})
    cid = packaging(gctx)
    sums = stage_artifacts(gctx.paths, cid, 1, out.out)
    sums["change.bundle"] = "e" * 64
    resp = await post(gctx, cid, package_request(gctx.paths, cid, 1, sums, out.head, world.base))
    assert resp.json()["error"] == "bundle_sha256"


# ------------------------------------------------------------------------------- AC 15 policy
@pytest.mark.parametrize(
    ("edits", "body", "code"),
    [
        ({".github/workflows/ci.yml": "on: push\n"}, None, "forbidden_path"),
        ({"LICENSE": "MIT\n"}, None, "forbidden_path"),
        ({"src/app.py": "key = 'FAKE_LEAK_123'\n"}, None, "secret_pattern"),
        ({"src/app.py": "print('ok')\n"}, "Fixes #1. no disclosure here", "missing_disclosure"),
        ({"src/app.py": "print('ok')\n"}, "mail me@corp.example <!-- ossbot:cid=x --> AI assistance disclosure",
         "confidential"),
    ],
)  # fmt: skip
async def test_policy_rejections(gctx: AppContext, world: GitWorld, edits: dict[str, str], body: str | None,
                                 code: str) -> None:  # fmt: skip
    out = world.package(world.base, edits)
    cid = packaging(gctx)
    sums = stage_artifacts(gctx.paths, cid, 1, out.out)
    resp = await post(gctx, cid, package_request(gctx.paths, cid, 1, sums, out.head, world.base, body=body))
    assert resp.status_code == 422
    assert resp.json()["error"] == "policy"
    assert code in resp.json()["detail"].split(",")
    assert state_of(gctx, cid) == "PACKAGING"


async def test_kiro_key_hash_in_patch_rejected(gctx: AppContext, world: GitWorld) -> None:
    key = "ksk" + "Q" * 30
    gctx.kiro_key_sha256 = sha256_text(key)
    out = world.package(world.base, {"src/app.py": f"token = {key}\n"})
    cid = packaging(gctx)
    sums = stage_artifacts(gctx.paths, cid, 1, out.out)
    resp = await post(gctx, cid, package_request(gctx.paths, cid, 1, sums, out.head, world.base))
    assert "kiro_key" in resp.json()["detail"].split(",")


@pytest.mark.parametrize(
    ("vetting", "override", "code"),
    [
        ({"ai_policy": "banned"}, None, "ai_policy_banned"),
        ({"ai_policy": "unclear"}, None, "ai_policy_unclear"),
        ({"ai_policy": "allowed", "cla_required": True}, None, "cla_required"),
        ({"ai_policy": "allowed"}, 0, "repo_disabled"),
    ],
)
async def test_repo_rejections(gctx: AppContext, world: GitWorld, vetting: dict[str, Any], override: int | None,
                               code: str) -> None:  # fmt: skip
    if override is not None:
        with db.transaction(gctx.conn):
            gctx.conn.execute(
                "INSERT INTO repo_overrides (upstream, enabled, updated_by, updated_at) VALUES (?,?,?,?)",
                ("langfuse/langfuse", override, "test", "2026-10-17T00:00:00Z"),
            )
    out = world.package(world.base, {"src/app.py": "print('r')\n"})
    cid = packaging(gctx, vetting_json=json.dumps(vetting))
    sums = stage_artifacts(gctx.paths, cid, 1, out.out)
    resp = await post(gctx, cid, package_request(gctx.paths, cid, 1, sums, out.head, world.base))
    assert code in resp.json()["detail"].split(",")


@pytest.mark.skipif(not os.path.exists("/usr/local/bin/gitleaks"), reason="gitleaks not installed")
def test_real_gitleaks_scanner() -> None:
    fake = "ghp_" + "Q8xk2LmZ9vR4tYp7" + "Wn3bJc6Hd1Fs5Ga0Ke8U"  # assembled at runtime: no secret-shaped literal in git
    assert gitleaks_scan(f'+token = "{fake}"\n'.encode()) == ["gitleaks"]
    assert gitleaks_scan(b"+print('hello')\n") == []


# ------------------------------------------------------------------- binding vs the mirror
async def test_upstream_moves_does_not_supersede(gctx: AppContext, world: GitWorld) -> None:
    """M12: upstream advancing never changes the binding; a force-push removing base fails the live check."""
    cid = packaging(gctx)
    out = world.package(world.base, {"src/app.py": "print('m12')\n"})
    doc = await package_ok(gctx, world, cid, out)
    approval = db.one(gctx.conn, "SELECT binding_hash FROM approvals WHERE id = ?", (doc["approval_id"],))
    world.advance_upstream()
    mirror = gctx.gitops.mirror_dir("langfuse/langfuse", cid, 1)
    assert live_checks.upstream_ancestry(gctx.gitops, "langfuse/langfuse", mirror, "main", world.base) == []
    from ossbot.gatekeeper.approvals import binding_hash, compute_binding

    assert (
        binding_hash(compute_binding(gctx.conn, doc["action_id"], gctx.approvals.artifacts)) == approval["binding_hash"]
    )
    row = db.one(gctx.conn, "SELECT upstream_base_tip FROM contributions WHERE id = ?", (cid,))
    assert row["upstream_base_tip"] == world.base
    world.force_push_upstream()
    assert live_checks.upstream_ancestry(gctx.gitops, "langfuse/langfuse", mirror, "main", world.base) == [
        "base_not_in_upstream"
    ]


@pytest.mark.parametrize("tamper", ["bundle_copy", "mirror_ref", "derived_patch"])
async def test_tampering_after_package_detected(gctx: AppContext, world: GitWorld, tamper: str) -> None:
    cid = packaging(gctx)
    out = world.package(world.base, {"src/app.py": "print('t')\n"})
    doc = await package_ok(gctx, world, cid, out)
    approval_id = doc["approval_id"]
    mirror = gctx.gitops.mirror_dir("langfuse/langfuse", cid, 1)
    if tamper == "bundle_copy":
        copy = gctx.gitops.work_bundle(cid, 1)
        copy.write_bytes(copy.read_bytes() + b"x")
    elif tamper == "mirror_ref":
        tgit(mirror, "update-ref", "refs/ossbot/r1", world.base)
    else:
        (gctx.gitops.derived_dir(cid, 1) / "change.patch").write_bytes(b"tampered")
    gctx.approvals.set_card_message(approval_id, 10)
    gctx.approvals.mark_part_delivered(approval_id)
    gctx.approvals.mark_part_delivered(approval_id)
    tokens = gctx.approvals.issue_card_tokens(approval_id)
    if tamper == "derived_patch":  # display copy only: the binding re-derives from the mirror
        arm = gctx.approvals.approve(tokens["approve"], tg_user_id=4242, chat_id=4242, message_id=10)
        assert arm.approval_id == approval_id
        return
    with pytest.raises(Superseded):
        gctx.approvals.approve(tokens["approve"], tg_user_id=4242, chat_id=4242, message_id=10)


def test_paths_are_gatekeeper_built(paths: Paths) -> None:
    ops = gitops.GitOps(paths)
    assert ops.work_bundle("c_0000000000", 3) == paths.gatekeeper_dir / "work" / "c_0000000000-r3.bundle"
    assert ops.mirror_dir("langfuse/langfuse", "c_0000000000", 3).name == "c_0000000000-r3.git"
    with pytest.raises(ValueError, match="bad upstream"):
        ops.cache_dir("../etc")
