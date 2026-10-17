"""Operator import (DESIGN §9.13, M3): bytes not paths; validation before any row exists."""

from __future__ import annotations

import base64
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from ossbot.common.hashing import sha256_hex
from ossbot.common.schemas import ImportRequest, State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from tests.conftest import UID_OPERATOR, UID_WORKER
from tests.githarness import GitWorld, tgit
from tests.unit.gatekeeper.api_helpers import client


def operator_bundle(world: GitWorld, *, branches: tuple[str, ...] = ("feature-x",), base: str | None = None) -> Path:
    base = base or world.base
    op = world.root / "operator"
    if op.exists():
        shutil.rmtree(op)
    tgit(world.root, "clone", "-q", str(world.upstream), str(op))
    tgit(op, "checkout", "-q", "-b", branches[0], base)
    world.write(op, {"src/feature.py": "x = 1\n"})
    world.commit(op, "operator work 1")
    world.write(op, {"src/feature.py": "x = 2\n"})
    head = world.commit(op, "operator work 2")
    for extra in branches[1:]:
        tgit(op, "branch", extra, head)
    bundle = world.root / "op.bundle"
    tgit(op, "bundle", "create", "-q", str(bundle), *(f"refs/heads/{b}" for b in branches), f"^{base}")
    return bundle


def request(data: bytes, base: str, **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "repo": "langfuse/langfuse",
        "base_ref": "main",
        "base_sha": base,
        "bundle_b64": base64.b64encode(data).decode(),
        "bundle_sha256": sha256_hex(data),
        "title": "feat: operator change\r\n",
        "body": "Prepared by the operator.\r\n",
    }
    body.update(over)
    return body


async def do_import(ctx: AppContext, body: dict[str, Any]) -> httpx.Response:
    async with client(ctx, UID_OPERATOR) as c:
        return await c.post("/v1/contributions/import", json=body)


def contribution_count(ctx: AppContext) -> int:
    return int(ctx.conn.execute("SELECT COUNT(*) FROM contributions").fetchone()[0])


def test_import_schema_has_no_path() -> None:
    assert not [name for name in ImportRequest.model_fields if "path" in name or "dir" in name]
    with pytest.raises(ValueError, match="bundle_path"):
        ImportRequest.model_validate(
            {"repo": "a/b", "base_ref": "main", "base_sha": "1" * 40, "bundle_b64": "AAAA", "bundle_sha256": "2" * 64,
             "title": "t", "body": "b", "bundle_path": "/etc/passwd"}
        )  # fmt: skip


async def test_import_valid_upload(gctx: AppContext, world: GitWorld) -> None:
    data = operator_bundle(world).read_bytes()
    resp = await do_import(gctx, request(data, world.base))
    assert resp.status_code == 201, resp.text
    cid = resp.json()["cid"]
    row = db.one(gctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
    assert row is not None
    assert (row["state"], row["source"], row["issue_number"]) == ("IMPORTED", "operator", None)
    assert row["import_head_ref"] == "refs/heads/feature-x"
    assert row["import_bundle_sha256"] == sha256_hex(data)
    assert (row["base_ref"], row["base_sha"]) == ("main", world.base)
    assert (row["pr_title"], row["pr_body"]) == ("feat: operator change", "Prepared by the operator.\n")
    stored = gctx.gitops.import_dir(cid) / "change.bundle"
    assert stored.read_bytes() == data
    assert stored.stat().st_mode & 0o777 == 0o600
    texts = [r["payload_json"] for r in gctx.conn.execute("SELECT payload_json FROM tg_outbox")]
    assert any("Local operator action: IMPORT" in t for t in texts)
    # no temporary import mirror is left behind
    assert not list((gctx.gitops.root / "work").glob("import-*"))


async def test_import_two_heads_rejected(gctx: AppContext, world: GitWorld) -> None:
    data = operator_bundle(world, branches=("feature-x", "feature-y")).read_bytes()
    resp = await do_import(gctx, request(data, world.base))
    assert resp.status_code == 422
    assert resp.json()["error"] == "import_invalid:heads"
    assert contribution_count(gctx) == 0


async def test_import_prerequisites_must_equal_base(gctx: AppContext, world: GitWorld) -> None:
    data = operator_bundle(world).read_bytes()
    newer = world.advance_upstream()
    resp = await do_import(gctx, request(data, newer))
    assert resp.json()["error"] == "import_invalid:prerequisites"
    assert contribution_count(gctx) == 0


async def test_import_base_not_upstream(gctx: AppContext, world: GitWorld) -> None:
    op = world.root / "op2"
    tgit(world.root, "clone", "-q", str(world.upstream), str(op))
    tgit(op, "checkout", "-q", "-b", "feature-x", world.base)
    world.write(op, {"a.txt": "local only\n"})
    local = world.commit(op, "never upstream")
    world.write(op, {"a.txt": "head\n"})
    world.commit(op, "head")
    bundle = world.root / "local.bundle"
    tgit(op, "bundle", "create", "-q", str(bundle), "refs/heads/feature-x", f"^{local}")
    resp = await do_import(gctx, request(bundle.read_bytes(), local))
    assert resp.json()["error"] == "import_invalid:base_not_upstream"
    assert contribution_count(gctx) == 0


async def test_import_head_not_descending_from_base(gctx: AppContext, world: GitWorld) -> None:
    op = world.root / "orphan"
    tgit(world.root, "clone", "-q", str(world.upstream), str(op))
    tgit(op, "checkout", "-q", "--orphan", "feature-x")
    world.write(op, {"other.txt": "unrelated history\n"})
    world.commit(op, "orphan")
    bundle = world.root / "orphan.bundle"
    tgit(op, "bundle", "create", "-q", str(bundle), "refs/heads/feature-x")
    raw = bundle.read_bytes()
    first, rest = raw.split(b"\n", 1)
    forged = first + b"\n-" + world.base.encode() + b" claimed base\n" + rest  # prerequisite line forged in
    resp = await do_import(gctx, request(forged, world.base))
    assert resp.json()["error"] == "import_invalid:head_not_descendant"
    assert contribution_count(gctx) == 0


@pytest.mark.parametrize("case", ["sha256", "size", "base64", "not_a_target"])
async def test_import_simple_rejections(
    gctx: AppContext, world: GitWorld, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = operator_bundle(world).read_bytes()
    body = request(data, world.base)
    if case == "sha256":
        body["bundle_sha256"] = "0" * 64
    elif case == "size":
        monkeypatch.setattr("ossbot.gatekeeper.imports.MAX_BUNDLE_BYTES", 32)
    elif case == "base64":
        body["bundle_b64"] = "!!!notbase64!!!"
    else:
        body["repo"] = "someone/unknown-repo"
    resp = await do_import(gctx, body)
    assert resp.status_code == 422
    assert resp.json()["error"] == f"import_invalid:{case}"
    assert contribution_count(gctx) == 0


async def test_import_worker_forbidden(gctx: AppContext, world: GitWorld) -> None:
    data = operator_bundle(world).read_bytes()
    async with client(gctx, UID_WORKER) as c:
        assert (await c.post("/v1/contributions/import", json=request(data, world.base))).status_code == 403


@pytest.mark.parametrize(
    ("state", "source", "status"),
    [
        (State.IMPORTED, "operator", 200),
        (State.FETCHING, "operator", 200),
        (State.FETCHED, "operator", 200),
        (State.VALIDATING, "operator", 409),
        (State.IMPORTED, "discover", 409),
    ],
)
async def test_import_bundle_download(
    gctx: AppContext, world: GitWorld, state: State, source: str, status: int
) -> None:
    data = operator_bundle(world).read_bytes()
    resp = await do_import(gctx, request(data, world.base))
    cid = resp.json()["cid"]
    with db.transaction(gctx.conn):
        gctx.conn.execute("UPDATE contributions SET state = ?, source = ? WHERE id = ?", (state.value, source, cid))
    async with client(gctx, UID_WORKER) as c:
        got = await c.get(f"/v1/contributions/{cid}/import-bundle")
    assert got.status_code == status
    if status == 200:
        assert got.content == data
        assert got.headers["content-type"] == "application/octet-stream"
    async with client(gctx, UID_OPERATOR) as c:
        assert (await c.get(f"/v1/contributions/{cid}/import-bundle")).status_code == 403


async def test_import_bundle_corrupt_store_refused(gctx: AppContext, world: GitWorld) -> None:
    data = operator_bundle(world).read_bytes()
    cid = (await do_import(gctx, request(data, world.base))).json()["cid"]
    (gctx.gitops.import_dir(cid) / "change.bundle").write_bytes(data + b"x")
    async with client(gctx, UID_WORKER) as c:
        got = await c.get(f"/v1/contributions/{cid}/import-bundle")
    assert got.status_code == 409
