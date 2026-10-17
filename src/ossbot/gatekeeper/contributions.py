"""Contribution creation with the re-discovery rules (DESIGN §9.4, M6) and read views."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any

from ossbot.common.clock import Clock, from_iso, to_iso
from ossbot.common.config import ConfigBundle, TargetsConfig, parse_yaml_model, resolve_target
from ossbot.common.ids import new_cid
from ossbot.common.rediscover import Decision, PriorContribution, RepoFactChanges, eligible
from ossbot.common.schemas import ContributionSource, State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.audit import AuditLog

REDACTED_FOR_REQUESTER = (
    "plan_json", "validation_json", "review_json", "vetting_json", "commit_message", "pr_body",
    "pr_body_override", "diffstat_json", "models_json", "package_ts_json",
)  # fmt: skip


class ContributionRefused(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        self.status = status
        self.code = code
        super().__init__(code)


def _cla_signed_at(conn: sqlite3.Connection, repo: str) -> str | None:
    """Load time of the earliest snapshot in the current trailing run with cla_signed true."""
    rows = conn.execute("SELECT files_json, loaded_at FROM config_snapshots ORDER BY loaded_at").fetchall()
    since: str | None = None
    for row in rows:
        try:
            targets = parse_yaml_model(
                TargetsConfig, "targets.yaml", json.loads(row["files_json"])["targets.yaml"].encode("utf-8")
            )
        except (ValueError, KeyError):
            since = None
            continue
        resolved = resolve_target(targets, repo)
        if resolved is not None and resolved.cla_signed:
            since = since or str(row["loaded_at"])
        else:
            since = None
    return since


def repo_fact_changes(conn: sqlite3.Connection, repo: str) -> RepoFactChanges:
    override = db.one(
        conn,
        "SELECT ai_policy_override, updated_at FROM repo_overrides WHERE lower(upstream) = lower(?)",
        (repo,),
    )
    ai_at = from_iso(override["updated_at"]) if override and override["ai_policy_override"] == "allowed" else None
    cla = _cla_signed_at(conn, repo)
    return RepoFactChanges(ai_allowed_at=ai_at, cla_signed_at=from_iso(cla) if cla else None)


def history(conn: sqlite3.Connection, repo: str, issue_number: int) -> list[PriorContribution]:
    rows = conn.execute(
        "SELECT id, state, state_reason, updated_at FROM contributions"
        " WHERE lower(upstream) = lower(?) AND issue_number = ? ORDER BY updated_at DESC",
        (repo, issue_number),
    ).fetchall()
    return [PriorContribution(r["id"], State(r["state"]), r["state_reason"], from_iso(r["updated_at"])) for r in rows]


class ContributionService:
    def __init__(
        self, conn: sqlite3.Connection, clock: Clock, audit: AuditLog, config: Callable[[], ConfigBundle]
    ) -> None:
        self.conn = conn
        self.clock = clock
        self.audit = audit
        self.config = config

    def decide(self, repo: str, issue_number: int) -> Decision:
        return eligible(
            history(self.conn, repo, issue_number),
            dry_run=db.current_dry_run(self.conn),
            now=self.clock.now(),
            cooldown_days=self.config().policy.limits.rediscover_cooldown_days,
            facts=repo_fact_changes(self.conn, repo),
        )

    def create(self, repo: str, issue_number: int, source: ContributionSource, score: float | None, actor: str) -> str:
        target = resolve_target(self.config().targets, repo)
        if target is None:
            raise ContributionRefused(400, "not_a_target")
        if not target.discovery and source != "operator":
            raise ContributionRefused(400, "discovery_disabled")
        with db.transaction(self.conn):
            decision = self.decide(repo, issue_number)
            if not decision.allowed:
                raise ContributionRefused(409, decision.http_code)
            cid = new_cid()
            now = to_iso(self.clock.now())
            initial = State.IMPORTED if source == "operator" else State.DISCOVERED
            try:
                self.conn.execute(
                    "INSERT INTO contributions (id, upstream, issue_number, issue_url, source, category, priority,"
                    " score, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        cid, target.repo, issue_number, f"https://github.com/{target.repo}/issues/{issue_number}",
                        source, target.category, target.priority, score, initial.value, now, now,
                    ),
                )  # fmt: skip
            except sqlite3.IntegrityError as exc:
                if "contributions_issue_active" in str(exc) or "UNIQUE" in str(exc):
                    raise ContributionRefused(409, "duplicate_active") from exc
                raise
            self.audit.append(
                actor,
                "contribution_created",
                {"repo": target.repo, "issue": issue_number, "source": source, "rule": decision.rule},
                cid,
            )
        return cid

    def get(self, cid: str, *, redacted: bool = False) -> dict[str, Any] | None:
        row = db.one(self.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
        if row is None:
            return None
        doc = dict(row)
        if redacted:
            for key in REDACTED_FOR_REQUESTER:
                doc.pop(key, None)
        return doc

    def list(self, state: str | None, limit: int) -> list[dict[str, Any]]:
        sql = "SELECT id, upstream, issue_number, source, state, state_reason, pr_number, pr_url, updated_at" \
              " FROM contributions"  # fmt: skip
        params: list[Any] = []
        if state:
            sql += " WHERE state = ?"
            params.append(state)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params)]

    def package_ts(self, cid: str, rev: int) -> str:
        """Idempotent per rev: the timestamp the package job uses for deterministic commits (§9.11)."""
        with db.transaction(self.conn):
            row = db.one(self.conn, "SELECT state, package_ts_json FROM contributions WHERE id = ?", (cid,))
            if row is None:
                raise ContributionRefused(404, "not_found")
            stamps: dict[str, str] = json.loads(row["package_ts_json"])
            key = str(rev)
            if key in stamps:
                return stamps[key]
            if row["state"] != State.PACKAGING.value:
                raise ContributionRefused(409, "not_packaging")
            stamps[key] = self.clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
            self.conn.execute(
                "UPDATE contributions SET package_ts_json = ? WHERE id = ?",
                (json.dumps(stamps, sort_keys=True), cid),
            )
            return stamps[key]
