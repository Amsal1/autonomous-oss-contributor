"""Gatekeeper retention (DESIGN §18.10, plan D1/D2): old closed Telegram rows and the data of long-terminal
contributions go; audit, approvals, contributions, actions and anything open or recent stay."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

from ossbot.common.clock import to_iso
from ossbot.common.ids import new_approval_id, new_proposal_approval_id, new_skill_approval_id, new_token
from ossbot.common.schemas import TERMINAL_STATES, State
from ossbot.gatekeeper import audit, db, retention
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.state_machine import ALLOWED, GATE, WAITING_W, WORKING
from tests.conftest import UID_OPERATOR, UID_WORKER, insert_action, insert_contribution, write_flag
from tests.unit.gatekeeper.api_helpers import client

NEVER_TOUCHED = ("audit_events", "approvals", "proposal_approvals", "skill_approvals", "contributions",
                 "outbound_actions", "proposals", "control_state", "config_snapshots", "policy_floor")  # fmt: skip
CHAT = 4242


def ago(ctx: AppContext, days: float) -> str:
    return to_iso(ctx.clock.now() - timedelta(days=days))


def later(ctx: AppContext, days: float) -> str:
    return to_iso(ctx.clock.now() + timedelta(days=days))


def old_terminal(ctx: AppContext, state: State = State.DRY_RUN_DONE, days: float = 31, **cols: object) -> str:
    return insert_contribution(ctx, state, updated_at=ago(ctx, days), **cols)


def make_dirs(ctx: AppContext, cid: str, upstream: str = "langfuse/langfuse") -> list[Path]:
    """The gatekeeper's copies of ``cid`` r1 (mirror, derived, import, work bundle) and the upstream cache."""
    ops = ctx.gitops
    mirror, derived, imported = ops.mirror_dir(upstream, cid, 1), ops.derived_dir(cid, 1), ops.import_dir(cid)
    for d in (mirror, derived, imported, ops.cache_dir(upstream)):
        d.mkdir(parents=True, exist_ok=True)
    (mirror / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    (derived / "change.patch").write_bytes(b"x" * 100)
    (imported / "change.bundle").write_bytes(b"y" * 10)
    (ops.cache_dir(upstream) / "HEAD").write_bytes(b"ref\n")
    bundle = ops.work_bundle(cid, 1)
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_bytes(b"z" * 7)
    return [mirror, ops.root / "derived" / cid, imported, bundle]


def approval(ctx: AppContext, action_id: str, status: str) -> str:
    aid = new_approval_id()
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO approvals (id, action_id, binding_json, binding_hash, dry_run_shown, status, tg_chat_id,"
            " body_parts_total, created_at, expires_at) VALUES (?,?,'{}',?,1,?,?,1,?,?)",
            (aid, action_id, "0" * 64, status, CHAT, ago(ctx, 200), later(ctx, 3)),
        )
    return aid


def proposal_approval(ctx: AppContext, cid: str, status: str) -> str:
    pid = new_proposal_approval_id()
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO proposal_approvals (id, cid, version, binding_json, binding_hash, status, tg_chat_id,"
            " created_at, expires_at) VALUES (?,?,1,'{}',?,?,?,?,?)",
            (pid, cid, "0" * 64, status, CHAT, ago(ctx, 200), later(ctx, 3)),
        )
    return pid


def skill_approval(ctx: AppContext, status: str) -> str:
    sid = new_skill_approval_id()
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO skill_approvals (id, skill_review_id, skill, version, binding_json, binding_hash, status,"
            " tg_chat_id, body_parts_total, created_at, expires_at) VALUES (?,'r_x','demo',1,'{}',?,?,?,1,?,?)",
            (sid, "0" * 64, status, CHAT, ago(ctx, 200), later(ctx, 3)),
        )
    return sid


def outbox_row(ctx: AppContext, status: str, days: float, approval_id: str | None = None) -> int:
    with db.transaction(ctx.conn):
        cur = ctx.conn.execute(
            "INSERT INTO tg_outbox (method, payload_json, approval_id, status, next_attempt_at)"
            " VALUES ('sendMessage', '{}', ?, ?, ?)",
            (approval_id, status, ago(ctx, days)),
        )
    assert cur.lastrowid is not None
    return cur.lastrowid


_MSG = iter(range(10_000, 99_999))


def view_row(ctx: AppContext, days: float, *, kind: str = "card", stale: int = 1, approval_id: str | None = None,
             action_id: str | None = None, cid: str | None = None) -> int:  # fmt: skip
    mid = next(_MSG)
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO tg_views (message_id, chat_id, kind, approval_id, cid, action_id, updated_at, stale)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (mid, CHAT, kind, approval_id, cid, action_id, ago(ctx, days), stale),
        )
    return mid


def token_row(ctx: AppContext, *, expires: str, used: str | None = None, approval_id: str | None = None) -> str:
    tok = new_token()
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO ui_tokens (token, purpose, approval_id, tg_chat_id, single_use, expires_at, used_at)"
            " VALUES (?, 'nav', ?, ?, 1, ?, ?)",
            (tok, approval_id, CHAT, expires, used),
        )
    return tok


def ids(ctx: AppContext, table: str, key: str) -> set[object]:
    return {r[0] for r in ctx.conn.execute(f"SELECT {key} FROM {table}")}


def row_counts(ctx: AppContext) -> dict[str, int]:
    tables = (*NEVER_TOUCHED, *retention.ROW_TABLES)
    return {t: int(ctx.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in tables}


# ------------------------------------------------------------------------------ selection
def test_terminal_list_matches_state_machine() -> None:
    for state in TERMINAL_STATES - {State.PROPOSAL_REJECTED}:  # its Undo lasts 10 min
        assert not ALLOWED.get(state), state
    assert not TERMINAL_STATES & (WORKING | GATE | WAITING_W | {State.PROPOSED, State.PROPOSAL_APPROVED,
                                                                 State.RESCOPE_REQUIRED})  # fmt: skip


def test_selects_terminal_contribution_dirs_after_30_days(ctx: AppContext) -> None:
    cid = old_terminal(ctx)
    paths = make_dirs(ctx, cid)
    report = retention.run(ctx, dry_run=False)
    assert report.eligible_cids == [cid]
    assert report.counts["mirrors"] == report.counts["derived"] == report.counts["imports"] == 1
    assert report.counts["work"] == report.counts["cache"] == 1 and report.counts["contributions"] == 1
    assert report.bytes["derived"] == 100 and report.bytes["imports"] == 10 and report.bytes["work"] == 7
    assert not any(p.exists() for p in paths)
    assert not ctx.gitops.cache_dir("langfuse/langfuse").exists()
    assert ids(ctx, "contributions", "id") == {cid}  # the row itself stays


NON_TERMINAL = sorted(set(State) - TERMINAL_STATES)


@pytest.mark.parametrize("state", NON_TERMINAL)
def test_never_selects_open_or_recent_contributions(ctx: AppContext, state: State) -> None:
    cid = insert_contribution(ctx, state, updated_at=ago(ctx, 400))
    recent = old_terminal(ctx, State.MERGED, days=29)
    paths = make_dirs(ctx, cid) + make_dirs(ctx, recent)
    report = retention.run(ctx, dry_run=False)
    assert report.eligible_cids == []
    assert all(p.exists() for p in paths)
    assert ctx.gitops.cache_dir("langfuse/langfuse").exists()


def test_open_states_include_the_gate_and_proposal_states() -> None:
    for state in (State.AWAITING_APPROVAL, State.APPROVED, State.SUBMITTING, State.SUBMITTED, State.SUBMIT_FAILED,
                  State.WATCHING, State.PROPOSED, State.RESCOPE_REQUIRED, State.PROPOSAL_SNOOZED,
                  State.PROPOSAL_EXPIRED):  # fmt: skip
        assert state in NON_TERMINAL


@pytest.mark.parametrize("case", ["open_action", "open_approval", "open_proposal_approval"])
def test_never_selects_terminal_contribution_with_open_action_or_approval(ctx: AppContext, case: str) -> None:
    cid = old_terminal(ctx)
    if case == "open_action":
        insert_action(ctx, cid, "comment", {"body": "x"}, status="pending_approval")
    elif case == "open_approval":
        approval(ctx, insert_action(ctx, cid, "comment", {"body": "x"}, status="done"), "confirmed")
    else:
        proposal_approval(ctx, cid, "armed")
    paths = make_dirs(ctx, cid)
    report = retention.run(ctx, dry_run=False)
    assert report.eligible_cids == [] and all(p.exists() for p in paths)


def test_closed_actions_and_approvals_do_not_block(ctx: AppContext) -> None:
    cid = old_terminal(ctx)
    approval(ctx, insert_action(ctx, cid, "comment", {"body": "x"}, status="dry_run_done"), "consumed")
    proposal_approval(ctx, cid, "consumed")
    assert retention.run(ctx, dry_run=True).eligible_cids == [cid]


def test_cache_repo_kept_while_upstream_has_open_contribution(ctx: AppContext) -> None:
    done = old_terminal(ctx)
    make_dirs(ctx, done)
    insert_contribution(ctx, State.PLANNING)  # same upstream, still working
    other = old_terminal(ctx, upstream="acme/widgets")
    make_dirs(ctx, other, upstream="acme/widgets")
    stray = ctx.gitops.cache_dir("nobody/unused")  # no contribution at all
    stray.mkdir(parents=True)
    report = retention.run(ctx, dry_run=False)
    assert sorted(report.eligible_cids) == sorted([done, other])
    assert ctx.gitops.cache_dir("langfuse/langfuse").exists()
    assert not ctx.gitops.cache_dir("acme/widgets").exists() and not stray.exists()
    assert report.counts["cache"] == 2


# --------------------------------------------------------------------------- Telegram rows
def test_outbox_only_closed_rows_older_than_cutoff(ctx: AppContext) -> None:
    gone = {outbox_row(ctx, s, 91) for s in ("sent", "failed", "merged")}
    kept = {outbox_row(ctx, "sent", 89), outbox_row(ctx, "pending", 200), outbox_row(ctx, "held", 200)}
    report = retention.run(ctx, dry_run=False)
    assert report.counts["tg_outbox"] == 3
    left = ids(ctx, "tg_outbox", "id")
    assert kept <= left and not gone & left


def test_outbox_row_of_open_approval_kept(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.AWAITING_APPROVAL)
    open_id = approval(ctx, insert_action(ctx, cid, "comment", {"body": "x"}), "pending")
    closed_id = approval(ctx, insert_action(ctx, cid, "comment", {"body": "y"}, status="done"), "consumed")
    kept = {outbox_row(ctx, "sent", 200, open_id), outbox_row(ctx, "sent", 200, proposal_approval(ctx, cid, "armed")),
            outbox_row(ctx, "sent", 200, skill_approval(ctx, "pending"))}  # fmt: skip
    gone = outbox_row(ctx, "sent", 200, closed_id)
    retention.run(ctx, dry_run=False)
    left = ids(ctx, "tg_outbox", "id")
    assert kept <= left and gone not in left


def test_views_keep_current_dashboard_open_approval_and_open_prompt(ctx: AppContext) -> None:
    live = insert_contribution(ctx, State.AWAITING_APPROVAL)
    open_action = insert_action(ctx, live, "comment", {"body": "x"})
    open_id = approval(ctx, open_action, "armed")
    done = old_terminal(ctx)
    done_action = insert_action(ctx, done, "comment", {"body": "x"}, status="done")
    kept = {
        view_row(ctx, 200, kind="dashboard", stale=0),
        view_row(ctx, 200, approval_id=open_id),
        view_row(ctx, 200, action_id=open_action),
        view_row(ctx, 200, cid=live),
        view_row(ctx, 89),
    }
    prompt = view_row(ctx, 200, kind="confirm_prompt")
    kept.add(prompt)
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO pending_input (id, kind, prompt_message_id, chat_id, created_at, expires_at)"
            " VALUES ('pi_1', 'changes', ?, ?, ?, ?)",
            (prompt, CHAT, ago(ctx, 0), later(ctx, 0.01)),
        )
    gone = {view_row(ctx, 200, kind="dashboard", stale=1), view_row(ctx, 200, action_id=done_action, cid=done),
            view_row(ctx, 91, kind="fyi")}  # fmt: skip
    report = retention.run(ctx, dry_run=False)
    left = ids(ctx, "tg_views", "message_id")
    assert kept <= left and not gone & left and report.counts["tg_views"] == 3


def test_tokens_only_old_and_never_open_approval(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.AWAITING_APPROVAL)
    open_id = approval(ctx, insert_action(ctx, cid, "comment", {"body": "x"}), "pending")
    gone = {token_row(ctx, expires=ago(ctx, 91)), token_row(ctx, expires=later(ctx, 400), used=ago(ctx, 91))}
    kept = {token_row(ctx, expires=ago(ctx, 89)), token_row(ctx, expires=later(ctx, 1)),
            token_row(ctx, expires=ago(ctx, 200), approval_id=open_id)}  # fmt: skip
    retention.run(ctx, dry_run=False)
    left = ids(ctx, "ui_tokens", "token")
    assert kept <= left and not gone & left


# --------------------------------------------------------------------------------- safety
def test_audit_rows_never_selected_and_chain_verifies_after_run(ctx: AppContext) -> None:
    cid = old_terminal(ctx)
    approval(ctx, insert_action(ctx, cid, "comment", {"body": "x"}, status="done"), "consumed")
    proposal_approval(ctx, cid, "rejected")
    skill_approval(ctx, "expired")
    make_dirs(ctx, cid)
    outbox_row(ctx, "sent", 200)
    before = row_counts(ctx)
    report = retention.run(ctx, dry_run=False)
    after = row_counts(ctx)
    assert report.counts["tg_outbox"] == 1 and report.eligible_cids == [cid]
    assert after["audit_events"] == before["audit_events"] + 1
    for table in NEVER_TOUCHED[1:]:
        assert after[table] == before[table], table
    assert audit.verify(ctx.conn).ok


def test_dry_run_deletes_nothing(ctx: AppContext) -> None:
    cid = old_terminal(ctx)
    paths = make_dirs(ctx, cid)
    outbox_row(ctx, "sent", 200)
    view_row(ctx, 200)
    token_row(ctx, expires=ago(ctx, 200))
    before = row_counts(ctx)
    dry = retention.run(ctx, dry_run=True)
    after = row_counts(ctx)
    assert dry.dry_run is True and all(p.exists() for p in paths)
    assert {t: after[t] for t in retention.ROW_TABLES} == {t: before[t] for t in retention.ROW_TABLES}
    real = retention.run(ctx, dry_run=False)
    assert real.counts == dry.counts and real.bytes == dry.bytes and real.eligible_cids == dry.eligible_cids
    assert not any(p.exists() for p in paths)


def test_run_appends_one_audit_event(ctx: AppContext) -> None:
    outbox_row(ctx, "sent", 200)
    for dry_run in (True, False):
        seq = ctx.audit.head().seq
        report = retention.run(ctx, dry_run=dry_run, actor="uid:0/operator")
        rows = ctx.conn.execute("SELECT actor, event, data_json FROM audit_events WHERE seq > ?", (seq,)).fetchall()
        assert [(r["actor"], r["event"]) for r in rows] == [("uid:0/operator", "retention_run")]
        assert json.loads(rows[0]["data_json"]) == {"dry_run": dry_run, "counts": report.counts}


@pytest.mark.parametrize("stop", ["killed", "paused"])
async def test_refused_while_killed_or_paused(ctx: AppContext, stop: str) -> None:
    outbox_row(ctx, "sent", 200)
    async with client(ctx, UID_WORKER) as c:
        assert (await c.post("/v1/retention/run", json={"dry_run": True})).status_code == 403
    async with client(ctx, UID_OPERATOR) as c:
        ok = await c.post("/v1/retention/run", json={"dry_run": True})
        assert ok.status_code == 200, ok.text
        assert ok.json()["counts"]["tg_outbox"] == 1 and ok.json()["snapshot_hash"] == ctx.snapshots.current.hash
        assert (await c.post("/v1/retention/run", json={"dry_run": True, "x": 1})).status_code == 400
        if stop == "killed":
            write_flag(ctx.paths.killed_flag, {"level": "soft", "reason": "x", "actor": "root"})
        else:
            ctx.control.pause("x", "test")
        seq = ctx.audit.head().seq
        refused = await c.post("/v1/retention/run", json={"dry_run": False})
    assert refused.status_code == 409 and refused.json() == {"error": "blocked", "state": stop}
    assert ctx.audit.head().seq == seq and len(ids(ctx, "tg_outbox", "id")) >= 1


def test_paths_stay_under_gitops_root(ctx: AppContext, tmp_path: Path) -> None:
    cid = old_terminal(ctx)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("precious")
    derived_root = ctx.gitops.root / "derived"
    derived_root.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, derived_root / cid)  # a symlinked derived/<cid> pointing outside
    work = ctx.gitops.root / "work"
    work.mkdir(parents=True, exist_ok=True)
    os.symlink(outside / "keep.txt", work / f"{cid}-r1.bundle")
    report = retention.run(ctx, dry_run=False)
    assert report.eligible_cids == [cid] and report.counts["derived"] == 0 and report.counts["work"] == 0
    assert (outside / "keep.txt").read_text() == "precious"
    assert (derived_root / cid).is_symlink() and (work / f"{cid}-r1.bundle").is_symlink()
