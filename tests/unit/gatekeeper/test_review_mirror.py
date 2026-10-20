"""The opt-in review mirror (ADR-0019 C, DESIGN §14.11.14; AC 24, AC 25, U4, U5, U9) against the fakes.

Real git plumbing in a temp bare repo; GitHub is ``fake_github``; the push itself is replaced (zero live
GitHub writes during the build).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ossbot.common.schemas import State
from ossbot.gatekeeper import db, review_mirror
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.gitops import Git
from ossbot.gatekeeper.review_mirror import (
    REVIEW_DIR,
    MirrorRefused,
    ReviewMirror,
    active_repo,
    build_snapshot,
    defang,
    integrity_ok,
    raw_diff,
)
from ossbot.gatekeeper.review_mirror_writer import MirrorWriter
from tests.conftest import packaged_contribution
from tests.fakes.fake_github import FakeGitHub

MIRROR = "test-bot-login/ossbot-review"
IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org", "GIT_COMMITTER_NAME": "t",
         "GIT_COMMITTER_EMAIL": "t@example.org"}  # fmt: skip
BASE_FILES = {"src/app.py": b"x = 1\n", "README.md": b"hello\n", "old.txt": b"gone soon\n"}
HEAD_FILES = {"src/app.py": b"x = 2\n", "README.md": b"hello\n", "tests/test_app.py": b"def test(): pass\n"}


def _commit(git: Git, repo: Path, files: dict[str, bytes], parent: str | None) -> str:
    index = repo / "t.index"
    env = {**IDENT, "GIT_INDEX_FILE": str(index)}
    git.run(["read-tree", "--empty"], cwd=repo, extra_env=env)
    for path, data in files.items():
        blob = git.run(["hash-object", "-w", "--stdin"], cwd=repo, stdin=data).stdout.decode().strip()
        git.run(["update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"], cwd=repo, extra_env=env)
    tree = git.run(["write-tree"], cwd=repo, extra_env=env).stdout.decode().strip()
    argv = ["commit-tree", tree, "-m", "c", *(["-p", parent] if parent else [])]
    sha = git.run(argv, cwd=repo, extra_env=env).stdout.decode().strip()
    index.unlink(missing_ok=True)
    return sha


def _repo(git: Git, repo: Path, head_files: dict[str, bytes] = HEAD_FILES) -> tuple[str, str]:
    repo.parent.mkdir(parents=True, exist_ok=True)
    git.run(["init", "-q", "--bare", str(repo)], cwd=repo.parent)
    base = _commit(git, repo, BASE_FILES, None)
    return base, _commit(git, repo, head_files, base)


def _tree(git: Git, repo: Path, commit: str) -> set[str]:
    return set(git.out(["ls-tree", "-r", "--name-only", commit], cwd=repo).splitlines())


# ------------------------------------------------------------------------------ plumbing (AC 24)
def test_snapshot_raw_diff_equals_base_head_outside_review_dir(tmp_path: Path) -> None:
    git = Git(home=tmp_path / "home")
    repo = tmp_path / "m.git"
    base, head = _repo(git, repo)
    snap = build_snapshot(git, repo, tmp_path / "work", base, head, {"PR_BODY.md": b"b", "COMMIT_MSG.txt": b"c",
                                                                    "REVIEW.md": b"r"}, 1_790_000_000)  # fmt: skip
    assert integrity_ok(git, repo, base, head, snap)
    assert _tree(git, repo, snap.base) == {"src/app.py", "old.txt"}  # exactly the touched paths at base
    assert _tree(git, repo, snap.head) == {"src/app.py", "tests/test_app.py", f"{REVIEW_DIR}/PR_BODY.md",
                                           f"{REVIEW_DIR}/COMMIT_MSG.txt", f"{REVIEW_DIR}/REVIEW.md"}  # fmt: skip
    assert git.out(["rev-list", "--parents", "-n1", snap.base], cwd=repo) == snap.base  # B' is a root commit
    assert git.out(["rev-parse", f"{snap.head}^"], cwd=repo) == snap.base  # H' has B' as its parent
    author = git.out(["log", "-1", "--format=%an <%ae> %at", snap.head], cwd=repo)
    assert author == "ossbot review mirror <noreply@invalid> 1790000000"
    assert not list((tmp_path / "work").glob("*.index"))  # the temporary index is gone


def test_a_tampered_blob_fails_the_integrity_check(tmp_path: Path) -> None:
    git = Git(home=tmp_path / "home")
    repo = tmp_path / "m.git"
    base, head = _repo(git, repo)
    snap = build_snapshot(git, repo, tmp_path / "work", base, head, {"REVIEW.md": b"r"}, 1)
    tampered_head = _commit(git, repo, {**HEAD_FILES, "src/app.py": b"x = 3  # not what was approved\n"}, base)
    bad = build_snapshot(git, repo, tmp_path / "work", base, tampered_head, {"REVIEW.md": b"r"}, 1)
    assert not integrity_ok(git, repo, base, head, bad)
    assert integrity_ok(git, repo, base, head, snap)
    assert {e.path for e in raw_diff(git, repo, base, head)} == {"src/app.py", "old.txt", "tests/test_app.py"}


def test_a_candidate_touching_the_review_dir_is_refused(tmp_path: Path) -> None:
    git = Git(home=tmp_path / "home")
    repo = tmp_path / "m.git"
    base, head = _repo(git, repo, {**HEAD_FILES, f"{REVIEW_DIR}/REVIEW.md": b"forged"})
    with pytest.raises(MirrorRefused):
        build_snapshot(git, repo, tmp_path / "work", base, head, {}, 1)


def test_mirror_text_has_no_mentions_refs_or_bare_urls() -> None:
    out = defang("Fixes #123, thanks @maintainer, see https://github.com/langfuse/langfuse/issues/9")
    assert "@" not in out and "#123" not in out
    assert "`https://github.com/langfuse/langfuse/issues/9`" in out


# ------------------------------------------------------------------------------ the service
def _enable_config(ctx: AppContext) -> None:
    """telegram.yaml review_mirror.enabled: true + profile review_mirror.repo, through a confirmed /reload."""
    cfg = ctx.paths.config_dir
    tg = cfg / "telegram.yaml"
    tg.write_text(tg.read_text().replace("review_mirror: {enabled: false", "review_mirror: {enabled: true"))
    profile = cfg / "profile.yaml"
    profile.write_text(profile.read_text() + f"review_mirror:\n  repo: {MIRROR}\n")
    ctx.snapshots.apply_reload(ctx.snapshots.prepare_reload().hash, "uid:1000/operator")


def _gh() -> FakeGitHub:
    gh = FakeGitHub()
    gh.add_repo(MIRROR, private=True, visibility="private")
    gh.actions_enabled[MIRROR.lower()] = False
    gh.add_repo("test-bot-login/dotfiles", private=False)
    return gh


def _svc(ctx: AppContext, gh: FakeGitHub) -> ReviewMirror:
    token = "github_pat_fake_mirror"
    svc = ReviewMirror(ctx, gh.client(token), token, classic=FakeGitHub().client())
    ctx.review_mirror = svc
    ctx.approvals.mirror_url = svc.card_url
    return svc


async def test_off_by_default_and_on_only_after_reload_and_enable(ctx: AppContext) -> None:
    """AC 25 / U9: config alone or the probe alone never turns the mirror on."""
    gh = _gh()
    svc = _svc(ctx, gh)
    assert active_repo(ctx) is None
    assert (await svc.enable("uid:1000/operator"))["error"] == "no_repo"  # nothing configured yet
    _enable_config(ctx)
    assert active_repo(ctx) is None  # config on, no passing probe yet
    result = await svc.enable("uid:1000/operator")
    assert result["ok"] and result["active"]
    assert active_repo(ctx) == MIRROR
    assert [r.path for r in gh.writes] == ["/repos/test-bot-login/dotfiles/git/refs"]  # only the probe


async def test_enable_refuses_a_token_that_can_write_elsewhere(ctx: AppContext) -> None:
    """AC 24: a foreign-write probe answering 422 refuses enable with a P0 alert."""
    _enable_config(ctx)
    gh = _gh()
    gh.create_ref_status = 422
    result = await _svc(ctx, gh).enable("uid:1000/operator")
    assert result == {"ok": False, "error": "token_writes_elsewhere"}
    assert active_repo(ctx) is None
    row = db.one(ctx.conn, "SELECT priority, payload_json FROM tg_outbox ORDER BY id DESC LIMIT 1")
    assert row is not None and row["priority"] == 0 and "revoke" in row["payload_json"]


@pytest.mark.parametrize(("repo_doc", "actions", "code", "prio"), [
    ({"private": False, "visibility": "public"}, False, "repo_public", 0),
    ({"private": True, "visibility": "private"}, True, "actions_enabled", 1),
])  # fmt: skip
async def test_public_repo_or_actions_disable_the_mirror(ctx: AppContext, repo_doc: dict[str, Any], actions: bool,
                                                         code: str, prio: int) -> None:  # fmt: skip
    _enable_config(ctx)
    gh = _gh()
    svc = _svc(ctx, gh)
    assert (await svc.enable("uid:1000/operator"))["ok"]
    gh.repos[MIRROR.lower()].update(repo_doc)
    gh.actions_enabled[MIRROR.lower()] = actions
    review_mirror.write_state(ctx, {**review_mirror.read_state(ctx), "checked_at": None})
    await svc.tick()
    assert active_repo(ctx) is None
    assert review_mirror.read_state(ctx)["disabled_reason"] == code
    row = db.one(ctx.conn, "SELECT priority FROM tg_outbox ORDER BY id DESC LIMIT 1")
    assert row is not None and row["priority"] == prio


Pushed = tuple[ReviewMirror, str, str, str, list[Any]]


async def _pushed(ctx: AppContext, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch,
                  head_files: dict[str, bytes] = HEAD_FILES) -> Pushed:  # fmt: skip
    _enable_config(ctx)
    svc = _svc(ctx, gh)
    assert (await svc.enable("uid:1000/operator"))["ok"]
    cid, action = packaged_contribution(ctx)
    repo = ctx.gitops.mirror_dir("langfuse/langfuse", cid, 1)
    base, head = _repo(ctx.gitops.git, repo, head_files)
    derived = ctx.gitops.derived_dir(cid, 1)
    derived.mkdir(parents=True, exist_ok=True)
    (derived / "meta.json").write_text(json.dumps({"round_base": base, "head": head}))
    with db.transaction(ctx.conn):
        approval = ctx.create_approval(action, "test")
    pushes: list[Any] = []
    monkeypatch.setattr(MirrorWriter, "push", lambda self, d, h, b, c: pushes.append((self.repo, h, b, c)))
    return svc, cid, action, approval, pushes


async def test_tick_pushes_opens_the_pr_and_card_row_4_links_it(ctx: AppContext,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    gh = _gh()
    svc, cid, _, approval, pushes = await _pushed(ctx, gh, monkeypatch)
    assert await svc.tick() == 1
    assert [p[0] for p in pushes] == [MIRROR] and pushes[0][3] == cid
    row = review_mirror.current_row(ctx.conn, cid, 1)
    assert row["tree_equal"] == 1 and row["pr_number"] is not None and row["last_error"] is None
    pr = gh.pulls[MIRROR.lower()][0]
    assert pr["title"].startswith(f"[{cid} r1] ") and pr["head"]["ref"] == f"review/{cid}"
    assert "@" not in pr["body"]
    tokens = dict.fromkeys(("approve", "reject", "explore", "files", "changes", "edit_text_pick"), "t" * 22)
    rows = ctx.approvals.card_rows(approval, tokens)
    assert rows[-1] == [("🐙 Review on GitHub ↗", f"https://github.com/{MIRROR}/pull/{row['pr_number']}")]
    assert review_mirror.explorer_line(ctx.conn, cid, 1) == f"🐙 Mirror PR #{row['pr_number']} · tree-equal ✅"
    assert all(r.path.startswith(f"/repos/{MIRROR}/") for r in gh.writes[1:])  # after the enable probe


async def test_a_tampered_package_is_never_pushed(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    gh = _gh()
    svc, cid, _, _, pushes = await _pushed(ctx, gh, monkeypatch)
    real = review_mirror.integrity_ok
    monkeypatch.setattr(review_mirror, "integrity_ok", lambda *a: False)
    await svc.tick()
    monkeypatch.setattr(review_mirror, "integrity_ok", real)
    assert pushes == []
    row = review_mirror.current_row(ctx.conn, cid, 1)
    assert row["pr_number"] is None and "integrity" in row["last_error"]
    assert review_mirror.explorer_line(ctx.conn, cid, 1) == "🐙 Mirror unavailable: integrity check failed"


async def test_a_github_approve_never_changes_an_approval(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC 24: an Approve review only gets "approvals only count here"."""
    gh = _gh()
    svc, cid, action, approval, _ = await _pushed(ctx, gh, monkeypatch)
    await svc.tick()
    number = int(review_mirror.current_row(ctx.conn, cid, 1)["pr_number"])
    gh.reviews[(MIRROR.lower(), number)] = [{"id": 7, "state": "APPROVED", "user": {"login": "test-bot-login"}}]
    await svc.poll(cid, 1, action)
    await svc.poll(cid, 1, action)
    assert db.one(ctx.conn, "SELECT status FROM approvals WHERE id = ?", (approval,))["status"] == "pending"
    assert db.one(ctx.conn, "SELECT state FROM contributions WHERE id = ?", (cid,))["state"] == "AWAITING_APPROVAL"
    notes = [r for r in ctx.conn.execute("SELECT payload_json FROM tg_outbox")
             if "approvals only count here" in r["payload_json"]]  # fmt: skip
    assert len(notes) == 1


async def test_a_submitted_review_becomes_one_change_request(ctx: AppContext,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    gh = _gh()
    svc, cid, action, approval, _ = await _pushed(ctx, gh, monkeypatch)
    await svc.tick()
    row = review_mirror.current_row(ctx.conn, cid, 1)
    number = int(row["pr_number"])
    patch = "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    (ctx.gitops.derived_dir(cid, 1) / "change.patch").write_text(patch)
    gh.reviews[(MIRROR.lower(), number)] = [
        {"id": 8, "state": "CHANGES_REQUESTED", "body": "Please rename x", "user": {"login": "test-bot-login"}},
        {"id": 9, "state": "COMMENTED", "body": "drive-by", "user": {"login": "someone-else"}},
    ]
    gh.review_comments[(MIRROR.lower(), number)] = [
        {"id": 31, "pull_request_review_id": 8, "path": "src/app.py", "line": 1, "side": "RIGHT",
         "commit_id": row["mirror_head"], "body": "x is vague", "user": {"login": "test-bot-login"}},
    ]  # fmt: skip
    assert await svc.poll(cid, 1, action) == 1
    assert db.one(ctx.conn, "SELECT state FROM contributions WHERE id = ?", (cid,))["state"] == \
        State.CHANGES_REQUESTED.value  # fmt: skip
    cr = db.one(ctx.conn, "SELECT text, comments_json FROM change_requests WHERE cid = ?", (cid,))
    assert "Please rename x" in cr["text"] and "src/app.py:1 — x is vague" in cr["text"]
    assert json.loads(cr["comments_json"])[0]["source"] == "mirror"
    assert db.one(ctx.conn, "SELECT status FROM approvals WHERE id = ?", (approval,))["status"] == "invalidated"
    assert await svc.poll(cid, 1, action) == 0  # never twice
