"""/package with SSH commit signing (owner addition 2026-10-21, ADR-0006 addendum; I1, I6, I12) on
local bare repos with the production git TCB, plus the submit-time refusal and the fix-round bundle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ossbot.common import gitcmd
from ossbot.common.schemas import State
from ossbot.gatekeeper import db, signing
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.derive import GitArtifacts
from tests.conftest import UID_OPERATOR, UID_WORKER, insert_contribution
from tests.fakes.fake_github import FakeGitHub
from tests.fakes.signing_key import key_body, needs_keygen
from tests.githarness import GitWorld, package_request, stage_artifacts, tgit
from tests.unit.gatekeeper.api_helpers import client
from tests.unit.gatekeeper.test_package_verify import VETTED, package_ok, post
from tests.unit.gatekeeper.test_size_report import patch_policy
from tests.unit.gatekeeper.test_submit import UPSTREAM, action, confirm, notes, submitter

pytestmark = [pytest.mark.unit, needs_keygen]
EMAIL = "test.person@example.org"
SIGNED_REQUIRED = json.dumps({"ai_policy": "allowed", "signed_commits_required": True})


@pytest.fixture
def body(tmp_path: Path) -> str:
    return key_body(tmp_path / "keys")


def with_key(ctx: AppContext, body: str) -> signing.CommitSigner:
    ctx.env = {**ctx.env, signing.SIGNING_ENV: body}
    ctx.reset_secret_scanner()
    signer = ctx.signer
    assert signer is not None
    return signer


def payload_of(ctx: AppContext, action_id: str) -> dict[str, Any]:
    row = db.one(ctx.conn, "SELECT payload_json FROM outbound_actions WHERE id = ?", (action_id,))
    assert row is not None
    doc: dict[str, Any] = json.loads(row["payload_json"])
    return doc


def git_out(repo: Path, *args: str) -> str:
    return tgit(repo, *args)


async def test_signed_package_binds_and_shows_the_signed_sha(gctx: AppContext, world: GitWorld, body: str) -> None:
    signer = with_key(gctx, body)
    cid = insert_contribution(gctx, State.PACKAGING, vetting_json=VETTED)
    out = world.package(world.base, {"src/app.py": "print('signed')\n"})
    doc = await package_ok(gctx, world, cid, out)
    signed = doc["head_sha"]
    assert signed != out.head
    mirror = gctx.gitops.mirror_dir(UPSTREAM, cid, 1)
    # the signed twin: same tree, parent, author, committer and message; good signature
    for fmt in ("%T", "%P", "%an <%ae> %ad", "%cn <%ce> %cd", "%B"):
        shown = [git_out(mirror, "log", "-1", f"--format={fmt}", sha) for sha in (signed, out.head)]
        assert shown[0] == shown[1], fmt
    assert signer.verify(gctx.gitops.git, mirror, signed, EMAIL)
    assert gctx.gitops.rev_parse(mirror, "refs/ossbot/r1") == signed
    # the approval binds the signed SHA, and the re-derivation at Approve/Confirm agrees with it
    payload = payload_of(gctx, doc["action_id"])
    assert payload["head_sha"] == signed
    assert payload["signature"] == {"format": "ssh", "fingerprint": signer.fingerprint}
    approval = db.one(gctx.conn, "SELECT binding_json FROM approvals WHERE id = ?", (doc["approval_id"],))
    assert json.loads(approval["binding_json"])["head_sha"] == signed
    hashes = GitArtifacts(gctx.gitops, gctx.conn).hashes(cid, 1, "open_pr")
    assert hashes.patch_sha256 == json.loads(approval["binding_json"])["patch_sha256"]
    meta = json.loads((gctx.gitops.derived_dir(cid, 1) / "meta.json").read_text())
    assert meta["head"] == signed and meta["signature"]["fingerprint"] == signer.fingerprint
    card = gctx.approvals.card_view(doc["approval_id"]).card
    assert f"🔏 Commit signed (SSH, key {signer.fingerprint})" in card
    assert "Commit not signed" not in card
    confirm(gctx, doc["approval_id"])  # Approve + Confirm re-derive from the mirror: no head_changed


async def test_no_key_stays_unsigned_as_today(gctx: AppContext, world: GitWorld) -> None:
    cid = insert_contribution(gctx, State.PACKAGING, vetting_json=VETTED)
    out = world.package(world.base, {"src/app.py": "print('plain')\n"})
    doc = await package_ok(gctx, world, cid, out)
    assert doc["head_sha"] == out.head
    assert "signature" not in payload_of(gctx, doc["action_id"])
    assert not gctx.gitops.signed_bundle(cid, 1).exists()
    assert "⚠️ Commit not signed" in gctx.approvals.card_view(doc["approval_id"]).card


async def test_required_without_a_key_refuses_and_off_never_signs(
    gctx: AppContext, world: GitWorld, body: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_policy(monkeypatch, gctx, commit={"signing": "required"})
    cid = insert_contribution(gctx, State.PACKAGING, vetting_json=VETTED)
    out = world.package(world.base, {"src/app.py": "print('req')\n"})
    sums = stage_artifacts(gctx.paths, cid, 1, out.out)
    resp = await post(gctx, cid, package_request(gctx.paths, cid, 1, sums, out.head, world.base))
    assert resp.status_code == 409 and resp.json()["error"] == "signing_key_missing"
    patch_policy(monkeypatch, gctx, commit={"signing": "off"})
    with_key(gctx, body)
    doc = await package_ok(gctx, world, cid, out)
    assert doc["head_sha"] == out.head


async def test_a_broken_key_fails_closed(gctx: AppContext, world: GitWorld) -> None:
    gctx.env = {**gctx.env, signing.SIGNING_ENV: "QUJD" * 40}
    cid = insert_contribution(gctx, State.PACKAGING, vetting_json=VETTED)
    out = world.package(world.base, {"src/app.py": "print('broken')\n"})
    sums = stage_artifacts(gctx.paths, cid, 1, out.out)
    resp = await post(gctx, cid, package_request(gctx.paths, cid, 1, sums, out.head, world.base))
    assert resp.status_code == 503 and resp.json()["error"] == "signing_key_invalid"
    assert "QUJD" not in resp.text


async def test_existing_contribution_repackaged_after_the_key_is_added_is_signed(
    gctx: AppContext, world: GitWorld, body: str
) -> None:
    """The live gate-2 case: r1 was packaged unsigned; the key is set; Request changes packages r2 signed."""
    cid = insert_contribution(gctx, State.PACKAGING, vetting_json=SIGNED_REQUIRED)
    first = world.package(world.base, {"src/app.py": "print('r1')\n"})
    doc1 = await package_ok(gctx, world, cid, first)
    assert doc1["head_sha"] == first.head
    signer = with_key(gctx, body)
    with db.transaction(gctx.conn):
        gctx.conn.execute("UPDATE contributions SET state = 'PACKAGING' WHERE id = ?", (cid,))
    second = world.package(world.base, {"src/app.py": "print('r2')\n"})
    doc2 = await package_ok(gctx, world, cid, second, rev=2)
    assert doc2["head_sha"] != second.head
    assert payload_of(gctx, doc2["action_id"])["signature"]["fingerprint"] == signer.fingerprint
    old = db.one(gctx.conn, "SELECT status, invalidated_reason FROM approvals WHERE id = ?", (doc1["approval_id"],))
    assert (old["status"], old["invalidated_reason"]) == ("invalidated", "new_package_rev")


async def test_signed_fix_round_unbundles_and_pushes_on_the_signed_head(
    gctx: AppContext, world: GitWorld, body: str
) -> None:
    """A PR whose head is the signed SHA: the next round unbundles the gatekeeper's signed bundle
    (served by GET head-bundle), builds on the signed head, and is signed again (push_update)."""
    from tests.unit.entrypoints.test_unbundle_script import _stage, _unbundle

    with_key(gctx, body)
    cid = insert_contribution(gctx, State.PACKAGING, vetting_json=VETTED)
    first = world.package(world.base, {"src/app.py": "print('fixed')\n"})
    doc = await package_ok(gctx, world, cid, first)
    signed1 = doc["head_sha"]
    async with client(gctx, UID_WORKER) as c:
        resp = await c.get(f"/v1/contributions/{cid}/head-bundle", params={"rev": 1})
    assert resp.status_code == 200
    bundle = world.root / "signed-r1.bundle"
    bundle.write_bytes(resp.content)
    branch = payload_of(gctx, doc["action_id"])["branch"]
    world.push_fork(bundle, signed1, branch)  # the earlier live submit pushed the signed SHA
    with db.transaction(gctx.conn):
        gctx.conn.execute(
            "UPDATE outbound_actions SET status = 'done', executed_at = ?, result_json = ? WHERE id = ?",
            ("2026-10-17T12:00:00.000000Z", json.dumps({"fork": "test-bot-login/langfuse"}), doc["action_id"]),
        )
        gctx.conn.execute("UPDATE contributions SET state = 'PACKAGING', pr_number = 41, head_sha = ?, branch = ?"
                          " WHERE id = ?", (signed1, branch, cid))  # fmt: skip
    repo = world.work / "repo"
    tgit(repo, "reset", "-q", "--soft", world.base)
    _stage(world, bundle, "change.bundle")
    code, result = _unbundle(world, "fix_round", gitcmd.CHANGE_REF, signed1)
    assert code == 0, result
    second = world.package(signed1, {"src/app.py": "print('fixed twice')\n"})
    doc2 = await package_ok(gctx, world, cid, second, rev=2, title=None, body=None)
    assert doc2["kind"] == "push_update"
    payload = payload_of(gctx, doc2["action_id"])
    assert payload["old_head_sha"] == signed1
    assert payload["head_sha"] != second.head and "signature" in payload


async def test_head_bundle_and_signing_routes(gctx: AppContext, world: GitWorld, body: str) -> None:
    cid = insert_contribution(gctx, State.PACKAGING, vetting_json=VETTED)
    out = world.package(world.base, {"src/app.py": "print('route')\n"})
    await package_ok(gctx, world, cid, out)
    async with client(gctx, UID_WORKER) as c:
        assert (await c.get(f"/v1/contributions/{cid}/head-bundle", params={"rev": 1})).json() == {
            "error": "not_signed"}  # fmt: skip
        assert (await c.get("/v1/contributions/c_0000000000/head-bundle", params={"rev": 1})).status_code == 404
        assert (await c.get("/v1/signing")).json() == {"mode": "auto", "available": False, "fingerprint": None}
    signer = with_key(gctx, body)
    async with client(gctx, UID_OPERATOR) as c:
        doc = (await c.get("/v1/signing")).json()
    assert doc == {"mode": "auto", "available": True, "fingerprint": signer.fingerprint}
    assert body not in json.dumps(doc)


# ------------------------------------------------------------------------- submit refusal
async def test_unsigned_rev_for_a_signed_commits_repo_is_refused_at_submit(gctx: AppContext, world: GitWorld) -> None:
    gh = FakeGitHub()
    gh.add_repo(UPSTREAM)
    gh.add_issue(UPSTREAM, 77)
    gctx.control.set_dry_run(False, "uid:1000/operator")
    cid = insert_contribution(gctx, State.PACKAGING, issue_number=77, vetting_json=SIGNED_REQUIRED)
    out = world.package(world.base, {"src/app.py": "print('unsigned')\n"})
    doc = await package_ok(gctx, world, cid, out)
    sub = submitter(gctx, gh)
    assert "unsigned_commit" in await sub.preflight(doc["approval_id"])
    confirm(gctx, doc["approval_id"])
    assert await sub.submit(doc["approval_id"]) == "failed"
    assert gh.writes == []
    assert action(gctx, doc["action_id"])["error_class"] == "unsigned_commit"
    assert any("requires signed commits" in n and "nothing was published" in n for n in notes(gctx))


async def test_signed_rev_for_a_signed_commits_repo_goes_through(gctx: AppContext, world: GitWorld, body: str) -> None:
    gh = FakeGitHub()
    gh.add_repo(UPSTREAM)
    gh.add_issue(UPSTREAM, 78)
    with_key(gctx, body)
    cid = insert_contribution(gctx, State.PACKAGING, issue_number=78, vetting_json=SIGNED_REQUIRED)
    out = world.package(world.base, {"src/app.py": "print('signed ok')\n"})
    doc = await package_ok(gctx, world, cid, out)
    sub = submitter(gctx, gh)
    assert "unsigned_commit" not in await sub.preflight(doc["approval_id"])
    confirm(gctx, doc["approval_id"])
    assert await sub.submit(doc["approval_id"]) == "dry_run_done"  # dry-run is the default
    assert gh.writes == []


def test_signed_bundle_goes_with_its_rev(gctx: AppContext) -> None:
    path = gctx.gitops.signed_bundle("c_0000000001", 3)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    gctx.gitops.delete_rev(UPSTREAM, "c_0000000001", 3)
    assert not path.exists()
