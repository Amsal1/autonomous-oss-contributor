"""Migrations (DESIGN §14.20.2): from an empty DB and from the previous number."""

from __future__ import annotations

import json
import sqlite3
from importlib import resources
from pathlib import Path

from ossbot.gatekeeper import db

DECISION_TABLES = {"proposal_approvals", "skill_approvals", "tg_views", "tg_thread_seq"}
OUTBOX_COLUMNS = {"result_message_id", "bind_tokens_json", "view_json"}
CR004_TABLES = {"runner_requests"}
HERMES_TABLES = {"skill_reviews", "chat_turns", "chat_sessions"}  # 0004_hermes.sql (FEAT-007)
UX_TABLES = {"tg_topics", "pending_input", "review_comments", "ux_prefs"}  # 0005_telegram_ux.sql (FEAT-009)
UX_OUTBOX_COLUMNS = {"priority", "thread_key", "coalesce_key", "merged_into"}
LATEST = 8
M2_TABLES = {"digest_runs", "reminders", "review_mirror", "telegraph_pages"}  # 0006_telegram_m2.sql (FEAT-010)
OPEN_TABLES = {"discovered_targets", "contribution_holds"}  # 0007_open_discovery.sql (post-v1)
MIGRATIONS = ((1, "0001_init.sql"), (2, "0002_decisions.sql"), (3, "0003_cr004.sql"), (4, "0004_hermes.sql"),
              (5, "0005_telegram_ux.sql"), (6, "0006_telegram_m2.sql"))  # fmt: skip


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_from_empty(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "a.db")
    assert db.migrate(conn) == LATEST
    assert _tables(conn) >= DECISION_TABLES | CR004_TABLES | HERMES_TABLES | UX_TABLES | M2_TABLES | OPEN_TABLES
    assert not {"pending_input_v7", "pending_input_v8"} & _tables(conn)
    assert {"owner_fix_rounds", "fixes_disabled_reason"} <= _columns(conn, "contributions")
    assert {"source", "source_ref", "untrusted_text"} <= _columns(conn, "change_requests")
    assert _columns(conn, "tg_outbox") >= OUTBOX_COLUMNS | UX_OUTBOX_COLUMNS
    assert "comments_json" in _columns(conn, "change_requests")
    assert db.migrate(conn) == LATEST  # idempotent


def test_from_version_4_keeps_outbox_rows(tmp_path: Path) -> None:
    """0005 adds the outbox priority/thread/coalesce columns to existing rows (P2 by default)."""
    conn = db.connect(tmp_path / "d.db")
    files = resources.files("ossbot.gatekeeper.migrations")
    for number, name in ((1, "0001_init.sql"), (2, "0002_decisions.sql"), (3, "0003_cr004.sql"),
                         (4, "0004_hermes.sql")):  # fmt: skip
        sql = (files / name).read_text(encoding="utf-8")
        conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
    conn.execute(
        "INSERT INTO tg_outbox (method, payload_json, status, next_attempt_at) VALUES ('m', '{}', 'pending', 'x')"
    )
    conn.execute("INSERT INTO change_requests (id, cid, text, created_at) VALUES ('r', 'c', 't', 'x')")
    assert db.migrate(conn) == LATEST
    row = conn.execute("SELECT priority, thread_key, coalesce_key, merged_into FROM tg_outbox").fetchone()
    assert tuple(row) == (2, None, None, None)
    assert conn.execute("SELECT comments_json FROM change_requests").fetchone()[0] is None
    kinds = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'pending_input'").fetchone()[0]
    for kind in ("changes", "edit_comment", "proposal_adjust", "proposal_snooze_days", "feature_fix"):
        assert f"'{kind}'" in kinds


def test_from_version_1_keeps_rows(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "b.db")
    sql = (resources.files("ossbot.gatekeeper.migrations") / "0001_init.sql").read_text(encoding="utf-8")
    conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = 1;\nCOMMIT;")
    conn.execute(
        "INSERT INTO tg_outbox (method, payload_json, status, next_attempt_at) VALUES ('m', '{}', 'sent', 'x')"
    )
    assert db.migrate(conn) == LATEST
    row = conn.execute("SELECT method, result_message_id, bind_tokens_json, view_json FROM tg_outbox").fetchone()
    assert tuple(row) == ("m", None, None, None)


def test_from_version_2_rebuilds_contributions_and_keeps_references(tmp_path: Path) -> None:
    """0003 rebuilds contributions (source 'user'); rows and the foreign keys onto it survive."""
    conn = db.connect(tmp_path / "c.db")
    files = resources.files("ossbot.gatekeeper.migrations")
    for number, name in ((1, "0001_init.sql"), (2, "0002_decisions.sql")):
        sql = (files / name).read_text(encoding="utf-8")
        conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
    conn.execute(
        "INSERT INTO contributions (id, upstream, issue_number, source, state, created_at, updated_at)"
        " VALUES ('c_0000000001', 'o/r', 5, 'discover', 'VETTED', 't', 't')"
    )
    conn.execute(
        "INSERT INTO stage_runs (id, cid, stage, attempt, status) VALUES ('r1', 'c_0000000001', 'vet', 1, 'succeeded')"
    )
    assert db.migrate(conn) == LATEST
    assert _tables(conn) >= CR004_TABLES | {"proposals", "outcomes", "discovery_prefs", "discovery_prefs_undo"}
    assert {"work_url", "notes", "issue_hash", "proposal_version", "size_report_json"} <= _columns(
        conn, "contributions"
    )
    assert conn.execute("SELECT state FROM contributions").fetchone()[0] == "VETTED"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.execute(
        "INSERT INTO contributions (id, upstream, source, state, created_at, updated_at)"
        " VALUES ('c_0000000002', 'o/r', 'user', 'DISCOVERED', 't', 't')"
    )
    import sqlite3

    try:
        conn.execute(
            "INSERT INTO stage_runs (id, cid, stage, attempt, status) VALUES ('r2', 'c_nope', 'vet', 1, 'queued')"
        )
    except sqlite3.IntegrityError:
        pass
    else:  # pragma: no cover - the foreign key must still be enforced on the rebuilt table
        raise AssertionError("foreign key onto contributions not enforced after the rebuild")


def _at_version_6(path: Path) -> sqlite3.Connection:
    conn = db.connect(path)
    files = resources.files("ossbot.gatekeeper.migrations")
    for number, name in MIGRATIONS:
        sql = (files / name).read_text(encoding="utf-8")
        conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
    return conn


def _insert_prompt(conn: sqlite3.Connection, pid: str, kind: str) -> None:
    conn.execute(
        "INSERT INTO pending_input (id, kind, cid, prompt_message_id, chat_id, created_at, expires_at)"
        " VALUES (?, ?, 'c_0000000001', 77, 4242, 't', 'z')",
        (pid, kind),
    )


def test_from_version_6_rebuilds_pending_input_and_adds_open_discovery(tmp_path: Path) -> None:
    """0007 (post-v1 open discovery): an open prompt survives the rebuild; the new kind and CHECKs hold."""
    conn = _at_version_6(tmp_path / "e.db")
    _insert_prompt(conn, "p1", "proposal_adjust")
    assert db.migrate(conn) == LATEST
    row = conn.execute("SELECT id, kind, cid, prompt_message_id, consumed_at FROM pending_input").fetchone()
    assert tuple(row) == ("p1", "proposal_adjust", "c_0000000001", 77, None)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert _tables(conn) >= OPEN_TABLES and "pending_input_v7" not in _tables(conn)
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"pending_input_open", "discovered_targets_status", "contribution_holds_open"} <= indexes
    _insert_prompt(conn, "p2", "open_topic_add")
    try:
        _insert_prompt(conn, "p3", "open_topic_remove")
    except sqlite3.IntegrityError:
        pass
    else:  # pragma: no cover
        raise AssertionError("unknown pending_input kind accepted")


def _at_version_7(path: Path) -> sqlite3.Connection:
    conn = _at_version_6(path)
    sql = (resources.files("ossbot.gatekeeper.migrations") / "0007_open_discovery.sql").read_text(encoding="utf-8")
    conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = 7;\nCOMMIT;")
    return conn


def _audit_transition(conn: sqlite3.Connection, cid: str, evidence: dict[str, object]) -> None:
    data = json.dumps({"from": "PACKAGING", "to": "WATCHING", "redirected_from": "FAILED", "evidence": evidence})
    conn.execute(
        "INSERT INTO audit_events (ts, actor, event, cid, data_json, prev_hash, row_hash)"
        " VALUES ('t', 'gatekeeper_internal', 'transition', ?, ?, 'p', 'h')",
        (cid, data),
    )


def test_from_version_7_adds_owner_fix_columns(tmp_path: Path) -> None:
    """0008 (post-v1 owner fix rounds): counters, the backfilled fixes-disabled flag, change-request
    source columns and the pending_input rebuild for 'pr_fix'."""
    conn = _at_version_7(tmp_path / "g.db")
    for issue, (cid, pr) in enumerate((("c_0000000001", 7), ("c_0000000002", 8), ("c_0000000003", None)), 5):
        conn.execute(
            "INSERT INTO contributions (id, upstream, issue_number, source, state, pr_number, created_at, updated_at)"
            " VALUES (?, 'o/r', ?, 'discover', 'WATCHING', ?, 't', 't')",
            (cid, issue, pr),
        )
    _audit_transition(conn, "c_0000000001", {"error_class": "branch_moved"})
    _audit_transition(conn, "c_0000000002", {"error_class": "patch_mismatch"})
    _audit_transition(conn, "c_0000000003", {"error_class": "branch_moved"})  # no PR: not a live fix
    _insert_prompt(conn, "p1", "changes")
    conn.execute("INSERT INTO change_requests (id, cid, text, created_at) VALUES ('r1', 'c_0000000001', 't', 'x')")
    assert db.migrate(conn) == LATEST
    rows = conn.execute("SELECT id, owner_fix_rounds, fixes_disabled_reason FROM contributions ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [
        ("c_0000000001", 0, "branch_moved"),
        ("c_0000000002", 0, None),
        ("c_0000000003", 0, None),
    ]
    request = conn.execute("SELECT source, source_ref, untrusted_text FROM change_requests").fetchone()
    assert tuple(request) == ("owner", None, None)
    prompt = conn.execute("SELECT id, kind, consumed_at FROM pending_input").fetchone()
    assert tuple(prompt) == ("p1", "changes", None)
    assert "pending_input_v8" not in _tables(conn)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "pending_input_open" in indexes
    _insert_prompt(conn, "p2", "pr_fix")
    bad = (
        ("INSERT INTO change_requests (id, cid, text, created_at, source) VALUES ('r2', 'c', 't', 'x', 'x')", ()),
        ("UPDATE contributions SET fixes_disabled_reason = 'x' WHERE id = 'c_0000000002'", ()),
    )
    for sql, params in bad:
        try:
            conn.execute(sql, params)
        except sqlite3.IntegrityError:
            continue
        raise AssertionError(f"CHECK not enforced: {sql}")  # pragma: no cover


def _discovered(conn: sqlite3.Connection, repo: str, **cols: object) -> None:
    row: dict[str, object] = {"repo": repo, "source": "search", "status": "candidate", "first_seen": "t",
                              "updated_at": "t", **cols}  # fmt: skip
    names = ", ".join(row)
    conn.execute(f"INSERT INTO discovered_targets ({names}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))


def test_open_discovery_checks(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "f.db")
    db.migrate(conn)
    _discovered(conn, "argoproj/argo-cd", ci_on_pr="yes", egress_strict=1)
    for bad in ({"status": "approved"}, {"source": "llm"}, {"ci_on_pr": "maybe"}, {"egress_strict": 2}):
        try:
            _discovered(conn, "o/bad", **bad)
        except sqlite3.IntegrityError:
            continue
        raise AssertionError(f"CHECK not enforced for {bad}")  # pragma: no cover
    try:  # repo is the key, case-insensitively
        _discovered(conn, "ArgoProj/Argo-CD")
    except sqlite3.IntegrityError:
        pass
    else:  # pragma: no cover
        raise AssertionError("repo key is not NOCASE")
    conn.execute(
        "INSERT INTO contributions (id, upstream, issue_number, source, state, created_at, updated_at)"
        " VALUES ('c_0000000001', 'o/r', 5, 'discover', 'FETCHING', 't', 't')"
    )
    hold = (
        "INSERT INTO contribution_holds (id, cid, reason, detail_json, held_by, held_at)"
        " VALUES (?, ?, ?, '{}', 'x', 't')"
    )
    conn.execute(hold, ("h1", "c_0000000001", "egress_anomaly"))
    for args in (("h2", "c_0000000001", "egress_anomaly"), ("h3", "c_0000000001", "other")):
        try:  # one open hold per cid; the reason is a closed set
            conn.execute(hold, args)
        except sqlite3.IntegrityError:
            continue
        raise AssertionError(f"contribution_holds accepted {args}")  # pragma: no cover
    conn.execute("UPDATE contribution_holds SET released_at = 't2', released_by = 'op' WHERE id = 'h1'")
    conn.execute(hold, ("h4", "c_0000000001", "egress_anomaly"))  # a new hold after the release
