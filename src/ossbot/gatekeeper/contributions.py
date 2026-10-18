"""Contribution creation with the re-discovery rules (DESIGN §9.4, M6) and read views."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from typing import Any

from ossbot.common.clock import Clock, from_iso, to_iso
from ossbot.common.config import ConfigBundle, TargetsConfig, parse_yaml_model, resolve_target
from ossbot.common.ids import new_cid
from ossbot.common.rediscover import Decision, PriorContribution, RepoFactChanges, eligible
from ossbot.common.schemas import TERMINAL_STATES, WORK_URL_RE, ContributionSource, State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.audit import AuditLog

REDACTED_FOR_REQUESTER = (
    "plan_json", "validation_json", "review_json", "vetting_json", "commit_message", "pr_body",
    "pr_body_override", "diffstat_json", "models_json", "package_ts_json", "notes", "proposal_feedback_json",
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
        "SELECT id, state, state_reason, updated_at, issue_hash FROM contributions"
        " WHERE lower(upstream) = lower(?) AND issue_number = ? ORDER BY updated_at DESC",
        (repo, issue_number),
    ).fetchall()
    return [
        PriorContribution(r["id"], State(r["state"]), r["state_reason"], from_iso(r["updated_at"]), r["issue_hash"])
        for r in rows
    ]


WORK_RE = re.compile(WORK_URL_RE)


class ContributionService:
    def __init__(
        self, conn: sqlite3.Connection, clock: Clock, audit: AuditLog, config: Callable[[], ConfigBundle]
    ) -> None:
        self.conn = conn
        self.clock = clock
        self.audit = audit
        self.config = config

    def decide(self, repo: str, issue_number: int, issue_hash: str | None = None) -> Decision:
        return eligible(
            history(self.conn, repo, issue_number),
            dry_run=db.current_dry_run(self.conn),
            now=self.clock.now(),
            cooldown_days=self.config().policy.limits.rediscover_cooldown_days,
            facts=repo_fact_changes(self.conn, repo),
            issue_hash=issue_hash,
        )

    def request_discover(self, repos: list[str], actor: str) -> dict[str, Any]:
        """``ossctl discover [repo...]`` (§9.4 on demand): held for the runner's next tick.

        Every repo must be an enabled target with discovery on; [] means all of them. Reads only
        happen in the runner; nothing is posted.
        """
        names: list[str] = []
        for repo in repos:
            target = resolve_target(self.config().targets, repo.removeprefix("https://github.com/").strip("/"))
            if target is None:
                raise ContributionRefused(400, "not_a_target")
            if not target.enabled or not target.discovery:
                raise ContributionRefused(400, "discovery_disabled")
            names.append(target.repo)
        now = to_iso(self.clock.now())
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO runner_requests (kind, requested_at, actor, payload) VALUES ('discover', ?, ?, ?)"
                " ON CONFLICT(kind) DO UPDATE SET requested_at = excluded.requested_at, actor = excluded.actor,"
                " payload = excluded.payload",
                (now, actor, json.dumps(sorted(set(names)))),
            )
            self.audit.append(actor, "discover_requested", {"repos": sorted(set(names))})
        return {"requested_at": now, "repos": sorted(set(names))}

    def claim_discover(self, actor: str) -> dict[str, Any]:
        """Runner: consume a pending discover request (``requested`` false when there is none)."""
        with db.transaction(self.conn):
            row = db.one(self.conn, "SELECT payload FROM runner_requests WHERE kind = 'discover'")
            if row is None:
                return {"requested": False, "repos": []}
            self.conn.execute("DELETE FROM runner_requests WHERE kind = 'discover'")
            repos = json.loads(row["payload"] or "[]")
            self.audit.append(actor, "discover_claimed", {"repos": repos})
        return {"requested": True, "repos": repos}

    def create_user(self, url: str, notes: str, actor: str) -> dict[str, Any]:
        """``/work`` / ``ossctl work`` (CR-004 Amendment B): user-directed, ``source='user'``.

        Only GitHub issue, PR or repo URLs of a configured, enabled target. Skips ranking, caps, the
        size preference, the filters and the re-discovery history (the owner asked for it); an
        active contribution for the same issue still answers 409 ``duplicate_active``."""
        clean = url.strip().split("#", 1)[0].split("?", 1)[0]
        match = WORK_RE.fullmatch(clean)
        if match is None:
            raise ContributionRefused(400, "not_a_github_url")
        repo = f"{match['owner']}/{match['repo']}"
        target = resolve_target(self.config().targets, repo)
        if target is None:
            raise ContributionRefused(400, "not_a_target")
        if not target.enabled:
            raise ContributionRefused(400, "target_disabled")
        ref = {"issues": "issue", "pull": "pull", None: "repo"}[match["kind"]]
        number = int(match["n"]) if match["n"] else None
        issue_number = number if ref == "issue" else None
        canonical = f"https://github.com/{target.repo}" + (f"/{match['kind']}/{number}" if number else "")
        with db.transaction(self.conn):
            if issue_number is not None and any(
                p.state not in TERMINAL_STATES for p in history(self.conn, target.repo, issue_number)
            ):
                raise ContributionRefused(409, "duplicate_active")
            cid = new_cid()
            now = to_iso(self.clock.now())
            self.conn.execute(
                "INSERT INTO contributions (id, upstream, issue_number, issue_url, source, category, priority,"
                " state, work_url, work_ref, notes, created_at, updated_at)"
                " VALUES (?,?,?,?,'user',?,?,?,?,?,?,?,?)",
                (
                    cid, target.repo, issue_number, canonical if ref == "issue" else None, target.category,
                    target.priority, State.DISCOVERED.value, canonical, ref, notes or None, now, now,
                ),
            )  # fmt: skip
            self.audit.append(actor, "work_requested", {"url": canonical, "ref": ref, "notes": bool(notes)}, cid)
        return {"cid": cid, "repo": target.repo, "ref": ref, "url": canonical}

    def create(
        self, repo: str, issue_number: int, source: ContributionSource, score: float | None, actor: str,
        issue_hash: str | None = None,
    ) -> str:  # fmt: skip
        target = resolve_target(self.config().targets, repo)
        if target is None:
            raise ContributionRefused(400, "not_a_target")
        if not target.discovery and source != "operator":
            raise ContributionRefused(400, "discovery_disabled")
        with db.transaction(self.conn):
            decision = self.decide(repo, issue_number, issue_hash)
            if not decision.allowed:
                raise ContributionRefused(409, decision.http_code)
            cid = new_cid()
            now = to_iso(self.clock.now())
            initial = State.IMPORTED if source == "operator" else State.DISCOVERED
            try:
                self.conn.execute(
                    "INSERT INTO contributions (id, upstream, issue_number, issue_url, source, category, priority,"
                    " score, state, issue_hash, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        cid, target.repo, issue_number, f"https://github.com/{target.repo}/issues/{issue_number}",
                        source, target.category, target.priority, score, initial.value, issue_hash, now, now,
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
        else:  # the owner's open change requests feed the next implement round (§9.8, trusted text)
            doc["change_requests"] = [
                {"id": r["id"], "text": r["text"], "created_at": r["created_at"]}
                for r in self.conn.execute(
                    "SELECT id, text, created_at FROM change_requests WHERE cid = ? AND consumed_at IS NULL"
                    " ORDER BY created_at",
                    (cid,),
                )
            ]
        return doc

    def list(self, state: str | None, limit: int, *, active: bool = False, offset: int = 0) -> list[dict[str, Any]]:
        """Newest first; ``active`` leaves out terminal items (the runner's dispatch view, paged
        with ``offset``, so long-waiting items never fall out of a window of recent ones)."""
        sql = "SELECT id, upstream, issue_number, source, state, state_reason, pr_number, pr_url, updated_at," \
              " kind, est_class, approved_class, proposal_version, snooze_until, work_url, work_ref," \
              " proposal_feedback_json IS NOT NULL AS feedback_pending FROM contributions"  # fmt: skip
        where: list[str] = []
        params: list[Any] = []
        if state:
            where.append("state = ?")
            params.append(state)
        if active:
            terminal = sorted(s.value for s in TERMINAL_STATES)
            where.append(f"state NOT IN ({', '.join('?' * len(terminal))})")
            params.extend(terminal)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, id LIMIT ? OFFSET ?"
        params.extend((limit, offset))
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
