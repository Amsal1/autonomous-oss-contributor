"""The skill gate end to end in the gatekeeper (DESIGN §15.5, §14.15; PLAN item 36): the root
hasher's reviews become skill cards bound to their bytes; tier changes and tools above the tier are
refused; Confirm is refused while killed; the activation request carries exactly the bound digest."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ossbot.common.control_flags import write_flag
from ossbot.ctl import skills_root as sr
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalError, Superseded
from ossbot.gatekeeper.context import AppContext
from tests.unit.gatekeeper.decision_helpers import CHAT, status_of

SKILL = "---\nname: protein-log\nallowed-tools: [memory]\n---\nLog protein per meal.\n"


def _stage(
    ctx: AppContext, tmp_path: Path, content: str = SKILL, pid: str = "abcd1234", tier: str = "self_improve"
) -> dict:
    pending, active = tmp_path / "pending", tmp_path / "active"
    pending.mkdir(exist_ok=True)
    active.mkdir(exist_ok=True)
    rec = {
        "id": pid,
        "subsystem": "skills",
        "summary": "log protein",
        "origin": "foreground",
        "created_at": 1.8e9,
        "payload": {"action": "create", "name": "protein-log", "content": content},
    }
    (pending / f"{pid}.json").write_text(json.dumps(rec), encoding="utf-8")
    store = ctx.paths.var_lib / "skills"
    store.parent.mkdir(parents=True, exist_ok=True)
    return sr.scan(tier, pending_dir=pending, active_dir=active, store=store)


def _deliver(ctx: AppContext, sid: str, message_id: int = 900) -> dict[str, str]:
    ctx.decisions.skills.set_card_message(sid, message_id)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE skill_approvals SET body_parts_delivered = body_parts_total WHERE id = ?", (sid,))
    return ctx.decisions.skills.issue_card_tokens(sid)


def _tap(message_id: int = 900) -> dict[str, int]:
    return {"tg_user_id": CHAT, "chat_id": CHAT, "message_id": message_id}


def test_staged_skill_becomes_a_bound_card_and_activation_carries_the_digest(ctx: AppContext, tmp_path: Path) -> None:
    doc = _stage(ctx, tmp_path)
    (sid,) = ctx.skill_reviews.sync()
    review = db.one(ctx.conn, "SELECT * FROM skill_reviews")
    assert review["status"] == "staged" and review["skill"] == "protein-log" and review["version"] == 1
    tokens = _deliver(ctx, sid)
    card = ctx.decisions.skills.card_view(sid)
    assert "protein-log" in card.card and "memory" in card.card and card.parts and "Log protein" in card.parts[0]
    arm = ctx.decisions.skills.approve(tokens["skill_approve"], **_tap())
    binding = ctx.decisions.skills.confirm(arm.confirm_token, **_tap())
    assert binding["content_sha256"] == doc["reviews"][0]["content_sha256"] and binding["tier"] == "self_improve"
    ctx.skill_reviews.request_activation(sid, f"tg:{CHAT}")
    req = json.loads((ctx.paths.control_dir / f"skill-request-{sid}.json").read_text())
    assert req == {
        "op": "activate",
        "review_id": review["id"],
        "approval_id": sid,
        "ts": req["ts"],
        "content_sha256": doc["reviews"][0]["content_sha256"],
    }
    assert status_of(ctx, "skill_approvals", sid) == "consumed"
    assert db.one(ctx.conn, "SELECT status FROM skill_reviews")["status"] == "activating"
    # root's decision comes back on the next sync
    (ctx.paths.var_lib / "skills" / "decisions.json").write_text(
        json.dumps({"decisions": [{"review_id": review["id"], "result": "activated"}]}), encoding="utf-8"
    )
    ctx.skill_reviews.sync()
    assert db.one(ctx.conn, "SELECT status FROM skill_reviews")["status"] == "activated"


def test_changed_bytes_supersede_the_card(ctx: AppContext, tmp_path: Path) -> None:
    _stage(ctx, tmp_path)
    (sid,) = ctx.skill_reviews.sync()
    tokens = _deliver(ctx, sid)
    (tmp_path / "pending" / "abcd1234.json").unlink()
    _stage(ctx, tmp_path, content=SKILL + "more\n", pid="abcd9999")
    (sid2,) = ctx.skill_reviews.sync()
    assert sid2 != sid
    assert status_of(ctx, "skill_approvals", sid) == "invalidated"
    with pytest.raises((ApprovalError, Superseded)):
        ctx.decisions.skills.approve(tokens["skill_approve"], **_tap())


def test_tools_above_the_tier_are_refused(ctx: AppContext, tmp_path: Path) -> None:
    _stage(ctx, tmp_path, content="---\nallowed-tools: [terminal]\n---\nrun things\n")
    assert ctx.skill_reviews.sync() == []  # no card: the skill would need more than the tier grants
    assert db.one(ctx.conn, "SELECT status FROM skill_reviews")["status"] == "staged"
    with pytest.raises(ApprovalError, match="tier_above"):
        ctx.decisions.skills.create(
            db.one(ctx.conn, "SELECT id FROM skill_reviews")["id"],
            "protein-log",
            1,
            tg_chat_id=CHAT,
            body_parts_total=1,
        )


def test_tier_change_invalidates_and_refuses(ctx: AppContext, tmp_path: Path) -> None:
    _stage(ctx, tmp_path)
    (sid,) = ctx.skill_reviews.sync()
    tokens = _deliver(ctx, sid)
    ctx.control.set_hermes_tier("research", "uid:0/operator", "self_improve")
    assert status_of(ctx, "skill_approvals", sid) == "invalidated"
    with pytest.raises((ApprovalError, Superseded)):
        ctx.decisions.skills.approve(tokens["skill_approve"], **_tap())
    review_id = db.one(ctx.conn, "SELECT id FROM skill_reviews")["id"]
    with pytest.raises(ApprovalError, match="tier_changed"):  # staged under self_improve, active tier research
        ctx.decisions.skills.create(review_id, "protein-log", 1, tg_chat_id=CHAT, body_parts_total=1)


def test_confirm_refused_while_killed(ctx: AppContext, tmp_path: Path) -> None:
    _stage(ctx, tmp_path)
    (sid,) = ctx.skill_reviews.sync()
    tokens = _deliver(ctx, sid)
    arm = ctx.decisions.skills.approve(tokens["skill_approve"], **_tap())
    write_flag(ctx.paths.killed_flag, {"level": "soft", "reason": "t", "actor": "t", "ts": "x"})
    with pytest.raises(ApprovalError, match="confirm_refused"):
        ctx.decisions.skills.confirm(arm.confirm_token, **_tap())
    os.unlink(ctx.paths.killed_flag)
    assert not list(ctx.paths.control_dir.glob("skill-request-*"))


def test_reject_requests_the_drop(ctx: AppContext, tmp_path: Path) -> None:
    _stage(ctx, tmp_path)
    (sid,) = ctx.skill_reviews.sync()
    tokens = _deliver(ctx, sid)
    ctx.decisions.skills.reject(tokens["skill_reject"], **_tap())
    ctx.skill_reviews.request_rejection(sid, f"tg:{CHAT}")
    req = json.loads((ctx.paths.control_dir / f"skill-request-{sid}.json").read_text())
    assert req["op"] == "reject" and "content_sha256" not in req
    assert db.one(ctx.conn, "SELECT status FROM skill_reviews")["status"] == "rejected"


def test_manifests_are_validated(ctx: AppContext, tmp_path: Path) -> None:
    store = ctx.paths.var_lib / "skills"
    store.mkdir(parents=True)
    bad = {
        "review_id": "r_" + "0" * 20,
        "pending_id": "abcd1234",
        "skill": "../x",
        "rel_dir": "x",
        "action": "create",
        "files": {},
        "content_sha256": "0" * 64,
        "declared_tools": [],
        "tier": "self_improve",
        "summary": "",
        "origin": "",
        "created_at": "",
        "diff": "",
        "extra": 1,
    }
    (store / "pending.json").write_text(json.dumps({"reviews": [bad]}), encoding="utf-8")
    assert ctx.skill_reviews.sync() == []
    assert db.one(ctx.conn, "SELECT COUNT(*) AS n FROM skill_reviews")["n"] == 0
