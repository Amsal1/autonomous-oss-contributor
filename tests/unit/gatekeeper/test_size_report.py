"""Size accounting at /package (CR-004 §9.14 c; DESIGN §14.11.17/18): measured vs the approved estimate,
overrun flagged by default, RESCOPE_REQUIRED only with reapprove or the hard ceiling."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from ossbot.common.config import ConfigBundle
from ossbot.common.hashing import canonical_json, sha256_hex
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.packaging import size_report
from ossbot.gatekeeper.proposals import ProposalService
from tests.githarness import GitWorld, package_request, stage_artifacts
from tests.unit.gatekeeper.test_package_verify import VETTED, post
from tests.unit.gatekeeper.test_proposals import sample

LINES = "".join(f"line {i}\n" for i in range(30))


def with_proposal(ctx: AppContext, approved: str = "xs", **est: Any) -> str:
    from tests.conftest import insert_contribution

    cid = insert_contribution(ctx, State.PACKAGING, vetting_json=VETTED, proposal_version=1, approved_class=approved,
                              est_class=approved, kind="bug")  # fmt: skip
    prop = sample(estimate={"files": 1, "code_lines": 2, "test_lines": 0, "class": approved, **est}, areas=["src/"])
    doc = canonical_json(prop).decode()
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO proposals (cid, version, proposal_json, proposal_sha256, est_class, created_by, created_at)"
            " VALUES (?, 1, ?, ?, ?, 'w', 't')",
            (cid, doc, sha256_hex(doc.encode()), approved),
        )
    return cid


def patch_policy(monkeypatch: pytest.MonkeyPatch, ctx: AppContext, **sections: Any) -> None:
    bundle = ctx.config
    policy = bundle.policy.model_copy(update={k: getattr(bundle.policy, k).model_copy(update=v)
                                              for k, v in sections.items()})  # fmt: skip
    patched: ConfigBundle = dataclasses.replace(bundle, policy=policy)
    monkeypatch.setattr(type(ctx), "config", property(lambda self: patched))


async def package(ctx: AppContext, world: GitWorld, cid: str, files: dict[str, str]) -> Any:
    out = world.package(world.base, files)
    sums = stage_artifacts(ctx.paths, cid, 1, out.out)
    return await post(ctx, cid, package_request(ctx.paths, cid, 1, sums, out.head, world.base))


def report_of(ctx: AppContext, cid: str) -> dict[str, Any]:
    row = db.one(ctx.conn, "SELECT size_report_json, state FROM contributions WHERE id = ?", (cid,))
    assert row is not None and row["size_report_json"]
    doc: dict[str, Any] = json.loads(row["size_report_json"])
    doc["_state"] = row["state"]
    return doc


async def test_overrun_is_flagged_not_blocked_by_default(gctx: AppContext, world: GitWorld) -> None:
    cid = with_proposal(gctx)
    resp = await package(gctx, world, cid, {"src/app.py": LINES, "docs/x.md": "doc\n", "tests/test_x.py": LINES})
    assert resp.status_code == 200, resp.text
    rep = report_of(gctx, cid)
    assert rep["_state"] == "AWAITING_APPROVAL"
    assert rep["measured"]["test_added"] == 30 and rep["measured"]["code_added"] >= 30  # test lines kept apart
    assert rep["overrun"] is True and rep["class_grew"] is True and rep["approved_class"] == "xs"
    assert rep["outside_areas"] == ["docs/x.md", "tests/test_x.py"]
    tracked = db.one(gctx.conn, "SELECT measured_class, size_class FROM outcomes WHERE cid = ?", (cid,))
    assert tracked is not None and tuple(tracked) == (rep["measured_class"], "xs")  # track record by class


async def test_reapprove_mode_sends_it_back_to_gate_1(
    gctx: AppContext, world: GitWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_policy(monkeypatch, gctx, size={"on_overrun": "reapprove"})
    cid = with_proposal(gctx)
    resp = await package(gctx, world, cid, {"src/app.py": LINES})
    assert resp.status_code == 202 and resp.json()["reason"] == "overrun"
    assert report_of(gctx, cid)["_state"] == "RESCOPE_REQUIRED"
    assert gctx.conn.execute("SELECT COUNT(*) FROM outbound_actions WHERE cid = ?", (cid,)).fetchone()[0] == 0
    # Continue at the measured class: back to PACKAGING with that class bound
    ProposalService(gctx).continue_rescope(cid, "tg:4242", at_measured=True)
    row = db.one(gctx.conn, "SELECT state, approved_class FROM contributions WHERE id = ?", (cid,))
    assert row is not None and row["state"] == "PACKAGING" and row["approved_class"] == "s"


async def test_reapprove_within_class_goes_to_preview(
    gctx: AppContext, world: GitWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_policy(monkeypatch, gctx, size={"on_overrun": "reapprove"})
    cid = with_proposal(gctx, approved="m")
    resp = await package(gctx, world, cid, {"src/app.py": LINES})
    assert resp.status_code == 200 and report_of(gctx, cid)["class_grew"] is False


@pytest.mark.parametrize("source", ["discover", "user"])
async def test_ceiling_pauses_the_item(
    gctx: AppContext, world: GitWorld, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    patch_policy(monkeypatch, gctx, diff={"max_files": 1})
    cid = with_proposal(gctx)
    with db.transaction(gctx.conn):
        gctx.conn.execute("UPDATE contributions SET source = ? WHERE id = ?", (source, cid))
    resp = await package(gctx, world, cid, {"src/app.py": "a\n", "src/b.py": "b\n"})
    assert resp.status_code == 202 and resp.json()["reason"] == "ceiling"
    assert report_of(gctx, cid)["_state"] == "RESCOPE_REQUIRED"
    texts = [r[0] for r in gctx.conn.execute("SELECT payload_json FROM tg_outbox")]
    assert any("Size ceiling reached" in t and "config only" in t for t in texts)


def test_report_without_a_proposal(ctx: AppContext) -> None:
    class E:
        def __init__(self, path: str, a: int, d: int) -> None:
            self.path, self.added, self.deleted = path, a, d

    row = {"approved_class": None, "source": "operator", "proposal_version": None, "id": "c_X"}
    rep = size_report(ctx, row, [E("src/a.py", 10, 2), E("package-lock.json", 900, 0), E("tests/test_a.py", 5, 0)])
    assert rep["measured"] == {"files": 3, "code_added": 10, "code_deleted": 2, "test_added": 5, "test_deleted": 0}
    assert rep["measured_class"] == "s" and rep["estimate"] is None and rep["overrun"] is False
