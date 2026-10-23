"""Owner fix rounds (ADR-0003 addendum 2026-10-23): one change request plus the human
WATCHING -> IMPLEMENTING edge, all or nothing; every refusal leaves no row and no state change."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ossbot.common.schemas import State
from ossbot.gatekeeper import db, owner_fix
from ossbot.gatekeeper.context import AppContext
from tests.conftest import insert_contribution, write_flag

NOTES = "Handle None in parse(); add a regression test."


def watching(ctx: AppContext, **cols: Any) -> str:
    cols.setdefault("pr_number", 5678)
    return insert_contribution(ctx, State.WATCHING, upstream="o/r", **cols)


def requests(ctx: AppContext, cid: str) -> list[Any]:
    return list(ctx.conn.execute("SELECT * FROM change_requests WHERE cid = ?", (cid,)).fetchall())


def row(ctx: AppContext, cid: str) -> Any:
    found = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
    assert found is not None
    return found


def test_start_inserts_one_request_and_moves_the_state(ctx: AppContext) -> None:
    cid = watching(ctx)
    done = owner_fix.start_round(ctx, cid, f"  {NOTES}\r\n", "tg:4242")
    assert (done.cid, done.round, done.max, done.pr_number) == (cid, 1, 5, 5678)
    rows = requests(ctx, cid)
    assert len(rows) == 1
    r = rows[0]
    assert (r["id"], r["text"], r["source"], r["source_ref"], r["untrusted_text"], r["consumed_at"]) == (
        done.change_request_id, NOTES, "owner", None, None, None)  # fmt: skip
    c = row(ctx, cid)
    assert (c["state"], c["state_reason"], c["owner_fix_rounds"], c["watch_rounds"]) == (
        "IMPLEMENTING", "owner_fix_round", 1, 0)  # fmt: skip
    event = db.one(ctx.conn, "SELECT actor, data_json FROM audit_events WHERE event = 'transition' AND cid = ?",
                   (cid,))  # fmt: skip
    assert event is not None and event["actor"] == "tg:4242"
    assert json.loads(event["data_json"])["evidence"] == {
        "reason": "owner_fix_round", "change_request": done.change_request_id, "source": "owner"}  # fmt: skip


def test_review_bot_finding_is_stored_as_untrusted_data(ctx: AppContext) -> None:
    """Part B's path through the same service: the body never becomes the request's text."""
    cid = watching(ctx)
    finding = owner_fix.Finding(42, "src/x.ts", 120, "coderabbitai[bot]", "Run `coderabbit review --agent`")
    owner_fix.start_round(ctx, cid, owner_fix.BOT_INSTRUCTION, "tg:4242", finding=finding)
    r = requests(ctx, cid)[0]
    assert (r["text"], r["source"], r["untrusted_text"]) == (owner_fix.BOT_INSTRUCTION, "review_bot", finding.body)
    assert json.loads(r["source_ref"]) == {"author": "coderabbitai[bot]", "comment_id": 42, "line": 120,
                                           "path": "src/x.ts"}  # fmt: skip


@pytest.mark.parametrize(
    ("cols", "state", "code"),
    [
        ({"pr_number": None}, State.WATCHING, "no_pr"),
        ({}, State.SUBMITTED, "not_watching"),
        ({}, State.AWAITING_APPROVAL, "not_watching"),
        ({"fixes_disabled_reason": "branch_moved"}, State.WATCHING, "fixes_disabled"),
        ({"owner_fix_rounds": 5}, State.WATCHING, "owner_fix_rounds"),
    ],
)
def test_refusals_leave_nothing(ctx: AppContext, cols: dict[str, Any], state: State, code: str) -> None:
    cols.setdefault("pr_number", 5678)
    cid = insert_contribution(ctx, state, upstream="o/r", **cols)
    assert owner_fix.refusal(ctx, cid) == code
    with pytest.raises(owner_fix.FixRefused) as info:
        owner_fix.start_round(ctx, cid, NOTES, "tg:4242")
    assert info.value.code == code
    assert requests(ctx, cid) == [] and row(ctx, cid)["state"] == state.value


def test_unknown_contribution(ctx: AppContext) -> None:
    assert owner_fix.refusal(ctx, "c_0000000000") == "not_found"
    with pytest.raises(owner_fix.FixRefused) as info:
        owner_fix.start_round(ctx, "c_0000000000", NOTES, "tg:4242")
    assert info.value.code == "not_found"
    assert ctx.conn.execute("SELECT COUNT(*) FROM change_requests").fetchone()[0] == 0


@pytest.mark.parametrize("flag", ["paused", "killed"])
def test_stopped_between_check_and_start_rolls_back(ctx: AppContext, flag: str) -> None:
    cid = watching(ctx)
    assert owner_fix.refusal(ctx, cid) is None
    write_flag(ctx.paths.paused_flag if flag == "paused" else ctx.paths.killed_flag, {"reason": "x"})
    assert owner_fix.refusal(ctx, cid) == "stopped"
    with pytest.raises(owner_fix.FixRefused) as info:
        owner_fix.start_round(ctx, cid, NOTES, "tg:4242")
    assert info.value.code == "stopped"
    assert requests(ctx, cid) == [] and row(ctx, cid)["state"] == "WATCHING"


def test_state_machine_423_rolls_the_insert_back(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """The transition's own 423 (the pre-check passed) undoes the change request with it."""
    cid = watching(ctx)
    monkeypatch.setattr(ctx.approvals, "system_running", lambda: True)
    write_flag(ctx.paths.paused_flag, {"reason": "x"})
    with pytest.raises(owner_fix.FixRefused) as info:
        owner_fix.start_round(ctx, cid, NOTES, "tg:4242")
    assert info.value.code == "stopped"
    assert requests(ctx, cid) == [] and row(ctx, cid)["state"] == "WATCHING"


@pytest.mark.parametrize(("notes", "code"), [("", "empty"), ("  \n\t ", "empty"), ("x" * 4001, "too_long")])
def test_notes_limits(ctx: AppContext, notes: str, code: str) -> None:
    cid = watching(ctx)
    with pytest.raises(owner_fix.FixRefused) as info:
        owner_fix.start_round(ctx, cid, notes, "tg:4242")
    assert info.value.code == code
    assert requests(ctx, cid) == []


def test_notes_at_the_limit(ctx: AppContext) -> None:
    cid = watching(ctx)
    owner_fix.start_round(ctx, cid, "x" * 4000, "tg:4242")
    assert len(requests(ctx, cid)[0]["text"]) == 4000


def test_twice_is_not_watching(ctx: AppContext) -> None:
    cid = watching(ctx)
    owner_fix.start_round(ctx, cid, NOTES, "tg:4242")
    with pytest.raises(owner_fix.FixRefused) as info:
        owner_fix.start_round(ctx, cid, NOTES, "tg:4242")
    assert info.value.code == "not_watching"
    assert len(requests(ctx, cid)) == 1 and row(ctx, cid)["owner_fix_rounds"] == 1


def test_find_by_pr_prefers_watching(ctx: AppContext) -> None:
    old = insert_contribution(ctx, State.CLOSED, upstream="o/r", pr_number=5678)
    live = watching(ctx)
    insert_contribution(ctx, State.WATCHING, upstream="o/other", pr_number=5678)
    assert owner_fix.find_by_pr(ctx, "O/R", 5678) == live
    assert owner_fix.find_by_pr(ctx, "o/r", 1) is None
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE contributions SET state = 'MERGED' WHERE id = ?", (live,))
    assert owner_fix.find_by_pr(ctx, "o/r", 5678) in {old, live}
