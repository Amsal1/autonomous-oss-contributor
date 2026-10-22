"""Repos found outside targets.yaml (post-v1 open discovery, design §8): reads, lifecycle, resolution.

``DiscoveredTargets`` owns every rule of §8.3 (who may move a row where) and is the only importer of
``discovered_targets_writer`` (import-linter contract); tracked ``config/targets.yaml`` is never
written. Every decision is audited; operator and human decisions and Hermes-suggestion skips are
announced as an FYI. Values come from GitHub through the runner (bounded by ``DiscoveredFacts``) and
from the owner; nothing here is LLM output.

``effective_target`` resolves a repo for ``/package`` and ``GET /v1/targets/resolve``: a config target
first, else a ``candidate``/``kept``/``never`` row, else any repo that already has a contribution created
for a discovered repo (category ``open-discovery``): an in-flight item keeps its target when its row later
moves to ``skipped`` (review-3 M1), while a repo removed from targets.yaml stays unresolved. Creating a new
contribution is stricter (``creatable_target``: ``candidate`` or ``kept`` only).
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ossbot.common.clock import Clock, from_iso, to_iso
from ossbot.common.config import (
    LANGUAGE_RE,
    OPEN_CATEGORY,
    ConfigBundle,
    ResolvedTarget,
    discovered_target,
    resolve_target,
)
from ossbot.common.schemas import (
    ISO_TS_RE,
    SPDX_ID_RE,
    DiscoveredDecisionName,
    DiscoveredFacts,
    DiscoveredView,
    PrefsUpdate,
)
from ossbot.gatekeeper import db, policy
from ossbot.gatekeeper import discovered_targets_writer as writer
from ossbot.gatekeeper.audit import AuditLog
from ossbot.gatekeeper.discovery_prefs import DiscoveryPrefs

RESOLVABLE = frozenset({"candidate", "kept", "never"})
CREATABLE = frozenset({"candidate", "kept"})
# D1 (plan): the states in which a contribution counts as "our open PR" for the one-open-PR rule; the
# same set as the runner's discover.ACTIVE_PR_STATES (listed here, never imported from the runner).
ACTIVE_PR_STATES: tuple[str, ...] = ("AWAITING_APPROVAL", "APPROVED", "SUBMITTING", "SUBMITTED", "WATCHING")
MAX_SUGGESTED_ROWS = 20  # §11: rows in status suggested at any time
MAX_SUGGESTIONS_PER_DAY = 10  # §11: per requester, rolling 24 h (audit rows)
UNTRY_MAX_DAYS = 30  # ossctl targets discovered --untry --since: at most this far back
# Fixed FYI texts for a Hermes suggestion that didn't pass (closed set; the code is shown otherwise).
SKIP_TEXTS: dict[str, str] = {
    "not_found": "the repo was not found",
    "fork": "it is a fork",
    "archived": "it is archived or disabled",
    "private": "it is not public",
    "stars_below_min": "it has fewer stars than the minimum",
    "stale_push": "it had no recent push",
    "license_missing": "it has no license",
    "license_not_osi": "its license is not OSI-approved",
    "issues_disabled": "its issues are disabled",
    "prs_disabled": "its pull requests are disabled",
    "pr_creation_restricted": "only collaborators may open pull requests",
    "no_contributing": "it has no CONTRIBUTING guide",
    "inactive_default_branch": "its default branch has had no commit in months",
    "egress_extra_needed": "it needs package hosts outside the allowlist",
    "egress_extra_needed_strict": "it needs package hosts outside the allowlist (strict egress)",
}


# Owner addition (2026-10-21): /work on any public repo. The hard checks applied before a non-target repo
# is kept for user-directed work (preferences and the activity gate don't apply; the latter only warns).
WORK_READ_TIMEOUT_S = 10.0
_LANGUAGE = re.compile(LANGUAGE_RE)
_SPDX = re.compile(SPDX_ID_RE)
_ISO = re.compile(ISO_TS_RE)


@dataclass(frozen=True)
class WorkRepoFacts:
    """``GET /repos/{r}`` (None = 404) and the default branch's latest commit date (None = unknown)."""

    doc: dict[str, Any] | None
    default_branch_commit_at: str | None = None


async def read_work_facts(reader: Any, repo: str, timeout_s: float | None = None) -> WorkRepoFacts | None:
    """Read the repo facts for ``/work`` with the gatekeeper's read-only client; None when GitHub can't be
    read (no client, an error or the timeout), which refuses the request rather than guessing."""
    if reader is None:
        return None
    try:
        async with asyncio.timeout(WORK_READ_TIMEOUT_S if timeout_s is None else timeout_s):
            doc = await reader.repo(repo)
            if doc is None:
                return WorkRepoFacts(None)
            branch = doc.get("default_branch")
            commit_at = None
            if isinstance(branch, str) and branch:
                with contextlib.suppress(Exception):  # unknown activity is not a refusal
                    commit_at = await reader.default_branch_commit_at(repo, branch)
    except Exception:  # noqa: BLE001 - any read failure means "couldn't check", never a pass
        return None
    return WorkRepoFacts(doc if isinstance(doc, dict) else None, commit_at)


def hard_repo_check(repo: str, doc: dict[str, Any] | None) -> str | None:
    """The refusal code for ``/work`` on a non-target repo, or None (exists and public, not archived,
    disabled or a fork, issues and pull requests on, PR creation open to everyone)."""
    if doc is None or str(doc.get("full_name") or "").lower() != repo.lower():
        return "not_found"
    if doc.get("private") is True or doc.get("visibility", "public") != "public":
        return "private"
    if doc.get("archived") or doc.get("disabled"):
        return "archived"
    if doc.get("fork"):
        return "fork"
    if doc.get("has_issues") is False:
        return "issues_disabled"
    if doc.get("has_pull_requests") is False:
        return "prs_disabled"
    if doc.get("pull_request_creation_policy") == "collaborators_only":
        return "pr_creation_restricted"
    return None


def activity_warning(facts: WorkRepoFacts, max_age_days: int, now: datetime) -> str | None:
    """``⚠️ last commit on main: 8 months ago`` when the default branch is older than the gate; else None."""
    doc = facts.doc or {}
    branch = str(doc.get("default_branch") or "the default branch")[:100]
    if facts.default_branch_commit_at is None or not _ISO.match(facts.default_branch_commit_at):
        return None
    days = (now - from_iso(facts.default_branch_commit_at)).days
    if days <= max_age_days:
        return None
    age = f"{days // 30} months ago" if days >= 60 else f"{days} days ago"
    return f"⚠️ last commit on {branch}: {age}"


def _work_facts(facts: WorkRepoFacts) -> dict[str, Any]:
    """The bounded GitHub facts stored on the kept row (values that don't fit the schema are dropped)."""
    doc = facts.doc or {}
    stars = doc.get("stargazers_count")
    language = doc.get("language")
    lic = doc.get("license")
    spdx = lic.get("spdx_id") if isinstance(lic, dict) else None
    pushed = doc.get("pushed_at")
    commit_at = facts.default_branch_commit_at
    return {
        "stars": stars if isinstance(stars, int) and 0 <= stars <= 10_000_000 else None,
        "language": language if isinstance(language, str) and _LANGUAGE.match(language) else None,
        "license_spdx": spdx if isinstance(spdx, str) and _SPDX.match(spdx) else None,
        "pushed_at": pushed if isinstance(pushed, str) and len(pushed) <= 40 and _ISO.match(pushed) else None,
        "default_branch_commit_at": commit_at if commit_at and len(commit_at) <= 40 and _ISO.match(commit_at) else None,
    }


class DiscoveredRefused(ValueError):
    """A refused write (``{"error": code}`` on the API, like ``PrefsRefused``)."""

    def __init__(self, status: int, code: str, detail: str = "") -> None:
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def view(row: sqlite3.Row | dict[str, Any]) -> DiscoveredView:
    data = dict(row)
    data["egress_strict"] = bool(data["egress_strict"])
    return DiscoveredView.model_validate(data)


class DiscoveredTargets:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        audit: AuditLog,
        config: Callable[[], ConfigBundle],
        prefs: DiscoveryPrefs,
        notify: Callable[[str], None] = lambda text: None,
    ) -> None:
        self.conn = conn
        self.clock = clock
        self.audit = audit
        self.config = config
        self.prefs = prefs
        self.notify = notify

    # ------------------------------------------------------------------- reads
    def _row(self, repo: str) -> sqlite3.Row | None:
        return db.one(self.conn, "SELECT * FROM discovered_targets WHERE repo = ?", (repo,))

    def get(self, repo: str) -> DiscoveredView | None:
        row = self._row(repo)
        return view(row) if row is not None else None

    def list(self, status: str | None = None, *, untried: bool = False, limit: int = 200) -> list[DiscoveredView]:
        where: list[str] = []
        params: list[Any] = []
        if status is not None:
            where.append("status = ?")
            params.append(status)
        if untried:
            where.append("tried_at IS NULL")
        sql = "SELECT * FROM discovered_targets"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY repo_score IS NULL, repo_score DESC, first_seen, repo LIMIT ?"
        params.append(limit)
        return [view(r) for r in self.conn.execute(sql, params)]

    def excluded(self) -> builtins.list[str]:  # ``list`` is the method above in this class scope
        """Lower-cased repos the open search must not pick (§5.2 rule 2): ``never``, ``kept``, rows in
        cooldown and untried candidates already queued. No logins (finding 11)."""
        now = to_iso(self.clock.now())
        rows = self.conn.execute(
            "SELECT lower(repo) AS r FROM discovered_targets WHERE status IN ('never', 'kept')"
            " OR (status IN ('candidate', 'skipped') AND cooldown_until IS NOT NULL AND cooldown_until > ?)"
            " OR (status = 'candidate' AND tried_at IS NULL) ORDER BY r",
            (now,),
        )
        return [str(r["r"]) for r in rows]

    def open_pr_count(self, repo: str) -> int:
        """Contributions of ``repo`` in ``AWAITING_APPROVAL…WATCHING`` (the one-open-PR rule, D1)."""
        row = db.one(
            self.conn,
            "SELECT COUNT(*) AS n FROM contributions WHERE upstream = ? COLLATE NOCASE AND state IN (?, ?, ?, ?, ?)",
            (repo, *ACTIVE_PR_STATES),
        )
        return int(row["n"]) if row is not None else 0

    # -------------------------------------------------------------- resolution
    def effective_target(self, repo: str) -> ResolvedTarget | None:
        """Config target, else a ``candidate``/``kept``/``never`` row, else (M1) any repo with a
        contributions row; ``suggested``/``skipped``/absent without a contribution → None."""
        targets = self.config().targets
        configured = resolve_target(targets, repo)
        if configured is not None:
            return configured
        row = self._row(repo)
        if row is not None and row["status"] in RESOLVABLE:
            return discovered_target(targets, str(row["repo"]), kept=row["status"] == "kept",
                                     egress_strict=bool(row["egress_strict"]))  # fmt: skip
        # M1: only contributions created for a discovered repo (category open-discovery); a repo removed
        # from targets.yaml keeps failing not_a_target for its old contributions, as before.
        contrib = db.one(
            self.conn,
            "SELECT upstream FROM contributions WHERE upstream = ? COLLATE NOCASE AND category = ? LIMIT 1",
            (repo, OPEN_CATEGORY),
        )
        if contrib is None:
            return None
        return discovered_target(
            targets,
            str(row["repo"]) if row is not None else str(contrib["upstream"]),
            kept=False,
            egress_strict=bool(row["egress_strict"]) if row is not None else False,
        )

    def creatable_target(self, repo: str) -> ResolvedTarget:
        """A new contribution on a non-config repo: status ``candidate`` or ``kept`` only (§8.5).

        ``never`` → 400 ``never_repo``; anything else (incl. denied) → 400 ``not_a_target``."""
        row = self._row(repo)
        if row is None or row["status"] not in CREATABLE:
            code = "never_repo" if row is not None and row["status"] == "never" else "not_a_target"
            raise DiscoveredRefused(400, code)
        target = discovered_target(self.config().targets, str(row["repo"]), kept=row["status"] == "kept",
                                   egress_strict=bool(row["egress_strict"]))  # fmt: skip
        if target is None:
            raise DiscoveredRefused(400, "not_a_target")
        return target

    # ------------------------------------------------------------------ checks
    def _denied(self, repo: str) -> bool:
        bundle = self.config()
        owner = repo.split("/", 1)[0].lower()
        return bundle.targets.is_denied(repo) or owner in policy.effective_forbidden_logins(self.conn, bundle.profile)

    def _is_target(self, repo: str) -> bool:
        return resolve_target(self.config().targets, repo) is not None

    def _cooldown(self) -> str:
        days = self.config().policy.discovery.open.repo_cooldown_days
        return to_iso(self.clock.now() + timedelta(days=days))

    def _cooling(self, row: sqlite3.Row) -> bool:
        until = row["cooldown_until"]
        return until is not None and from_iso(str(until)) > self.clock.now()

    # ---------------------------------------------------------- worker writes
    def record(self, facts: DiscoveredFacts, actor: str) -> DiscoveredView:
        """The runner's result for one repo (§8.3 worker rows)."""
        repo = facts.repo
        if self._is_target(repo):
            raise DiscoveredRefused(409, "is_target")
        if self._denied(repo):
            raise DiscoveredRefused(400, "denied")
        now = to_iso(self.clock.now())
        values = {k: getattr(facts, k) for k in writer.FACT_COLUMNS}
        with db.transaction(self.conn):
            row = self._row(repo)
            if row is not None and row["status"] == "never":
                raise DiscoveredRefused(409, "never_repo")
            if facts.kept_check:
                return self._kept_check(row, facts, values, now, actor)
            if row is not None and row["status"] == "kept":
                raise DiscoveredRefused(409, "is_kept")
            if row is not None and row["status"] == "skipped" and self._cooling(row):
                raise DiscoveredRefused(409, "cooling")
            source: str = facts.source
            before_status: str | None = None
            before_reason: str | None = None
            if row is None:
                new = {"repo": repo, "source": facts.source, "status": facts.status, "topic": facts.topic,
                       **values, "first_seen": now, "updated_at": now}  # fmt: skip
                writer.insert_row(self.conn, new)
                key = repo
            else:
                key = str(row["repo"])
                before_status, before_reason, source = row["status"], row["skip_reason"], str(row["source"])
                writer.update_facts(self.conn, key, values, now, topic=facts.topic)
            if facts.status == "skipped":
                writer.set_status(self.conn, key, "skipped", now, skip_reason=facts.skip_reason,
                                  cooldown_until=self._cooldown())  # fmt: skip
            else:
                writer.set_status(self.conn, key, "candidate", now, skip_reason=None)
                if facts.tried:
                    writer.set_tried(self.conn, key, now, self._cooldown())
            data = {"repo": key, "status": facts.status, "from": before_status, "source": source,
                    "skip_reason": facts.skip_reason}  # fmt: skip
            if facts.status == "candidate" and before_status != "candidate":
                self.audit.append(actor, "discovered_target_recorded", data)
            elif facts.status == "skipped" and (before_status is None or facts.skip_reason != before_reason):
                self.audit.append(actor, "discovered_target_skipped", data)
                if source == "hermes":
                    reason = str(facts.skip_reason)
                    text = SKIP_TEXTS.get(reason.split(":", 1)[0], f"it did not pass the checks ({reason})")
                    self.notify(f"🔎 Hermes suggestion {key} not used: {text}.")
            out = self._row(key)
        assert out is not None
        return view(out)

    def _kept_check(
        self, row: sqlite3.Row | None, facts: DiscoveredFacts, values: dict[str, Any], now: str, actor: str
    ) -> DiscoveredView:
        """A kept repo's re-check (§6.2): facts refreshed, ``skip_reason`` set or cleared; status stays."""
        if row is None or row["status"] != "kept":
            raise DiscoveredRefused(409, "not_kept")
        key = str(row["repo"])
        reason = facts.skip_reason if facts.status == "skipped" else None
        writer.update_facts(self.conn, key, values, now, topic=facts.topic)
        writer.set_checked(self.conn, key, now, reason)
        if reason != row["skip_reason"]:
            self.audit.append(actor, "discovered_target_checked", {"repo": key, "skip_reason": reason})
        out = self._row(key)
        assert out is not None
        return view(out)

    # ------------------------------------------------------- requester writes
    def suggest(self, repo: str, issue: int | None, actor: str) -> DiscoveredView:
        """A Hermes suggestion of a non-target repo (§11): a ``suggested`` row (``source=hermes`` on
        insert only). Callers handle config targets and ``candidate``/``kept`` rows themselves."""
        if self._is_target(repo):
            raise DiscoveredRefused(409, "is_target")
        if self._denied(repo):
            raise DiscoveredRefused(400, "denied")
        now_dt = self.clock.now()
        now = to_iso(now_dt)
        with db.transaction(self.conn):
            row = self._row(repo)
            if row is not None:
                status = str(row["status"])
                if status == "never":
                    raise DiscoveredRefused(409, "never_repo")
                if status == "skipped" and self._cooling(row):
                    raise DiscoveredRefused(409, "skipped", str(row["skip_reason"] or ""))
                if status in CREATABLE:
                    raise DiscoveredRefused(409, "already_found", status)
            since = to_iso(now_dt - timedelta(hours=24))
            recent = db.one(
                self.conn,
                "SELECT COUNT(*) AS n FROM audit_events WHERE event = 'discovered_target_suggested' AND actor = ?"
                " AND ts > ?",
                (actor, since),
            )
            pending = db.one(self.conn, "SELECT COUNT(*) AS n FROM discovered_targets WHERE status = 'suggested'")
            new_pending = row is None or row["status"] != "suggested"
            if (recent is not None and int(recent["n"]) >= MAX_SUGGESTIONS_PER_DAY) or (
                new_pending and pending is not None and int(pending["n"]) >= MAX_SUGGESTED_ROWS
            ):
                raise DiscoveredRefused(429, "suggestion_limit")
            if row is None:
                new = {"repo": repo, "source": "hermes", "status": "suggested", "suggested_issue": issue,
                       "first_seen": now, "updated_at": now}  # fmt: skip
                writer.insert_row(self.conn, new)
                key = repo
            else:
                key = str(row["repo"])
                writer.set_status(self.conn, key, "suggested", now, suggested_issue=issue, skip_reason=None,
                                  cooldown_until=None)  # fmt: skip
            self.audit.append(actor, "discovered_target_suggested", {"repo": key, "issue": issue})
            out = self._row(key)
        assert out is not None
        return view(out)

    # ------------------------------------------------- operator / human writes
    def decide(self, repo: str, decision: DiscoveredDecisionName, actor: str) -> dict[str, Any]:
        """Keep, Never, Unblock, Unkeep and the strict override (§8.3). Config targets: Never and
        Unblock route to ``discovery_prefs`` (``never`` on and off); Keep → 409 ``already_target``;
        everything else → 409 ``is_target`` (edit targets.yaml)."""
        target = resolve_target(self.config().targets, repo)
        if target is not None:
            if decision in ("never", "unblock"):
                self.prefs.update(PrefsUpdate(scope=target.repo, filters={"never": decision == "never"}), actor)
                return {"routed": "discovery_prefs", "repo": target.repo, "decision": decision}
            raise DiscoveredRefused(409, "already_target" if decision == "keep" else "is_target")
        if self._denied(repo):
            raise DiscoveredRefused(400, "denied")
        now = to_iso(self.clock.now())
        with db.transaction(self.conn):
            row = self._row(repo)
            key = str(row["repo"]) if row is not None else repo
            status = str(row["status"]) if row is not None else None
            if decision == "keep":
                if row is not None and status == "kept":
                    return {"view": view(row).model_dump()}
                if row is None:
                    writer.insert_row(self.conn, {"repo": key, "source": "owner", "status": "kept",
                                                  "decided_by": actor, "decided_at": now, "first_seen": now,
                                                  "updated_at": now})  # fmt: skip
                else:
                    writer.set_status(self.conn, key, "kept", now, decided_by=actor, decided_at=now,
                                      cooldown_until=None, checked_at=None)  # fmt: skip
                self._announce(actor, "discovered_target_kept", key, status,
                               f"⭐ Kept {key} as a target (open discovery): scanned every cycle.")  # fmt: skip
            elif decision == "never":
                if row is not None and status == "never":
                    return {"view": view(row).model_dump()}
                if row is None:
                    writer.insert_row(self.conn, {"repo": key, "source": "owner", "status": "never",
                                                  "decided_by": actor, "decided_at": now, "first_seen": now,
                                                  "updated_at": now})  # fmt: skip
                else:
                    writer.set_status(self.conn, key, "never", now, decided_by=actor, decided_at=now)
                self._announce(actor, "discovered_target_never", key, status,
                               f"🚫 Never {key}: it won't be searched or proposed again.")  # fmt: skip
            elif decision == "unblock":
                if row is None or status != "never":
                    raise DiscoveredRefused(409, "not_never")
                to = "candidate" if row["stars"] is not None else "suggested"
                writer.set_status(self.conn, key, to, now, decided_by=actor, decided_at=now, cooldown_until=None)
                self._announce(actor, "discovered_target_unblocked", key, status, f"↩️ Unblocked {key} ({to}).")
            elif decision == "unkeep":
                if row is None or status != "kept":
                    raise DiscoveredRefused(409, "not_kept")
                writer.set_status(self.conn, key, "candidate", now, decided_by=actor, decided_at=now)
                self._announce(actor, "discovered_target_unkept", key, status,
                               f"↩️ {key} is no longer a kept target.")  # fmt: skip
            else:  # strict / unstrict
                if row is None:
                    raise DiscoveredRefused(404, "not_found")
                strict = decision == "strict"
                if bool(row["egress_strict"]) != strict:
                    writer.set_strict(self.conn, key, strict, now, actor)
                    self.audit.append(actor, "discovered_target_egress", {"repo": key, "egress_strict": strict})
                    self.notify(f"🔒 {key}: strict egress {'on' if strict else 'off'}.")
            out = self._row(key)
        assert out is not None
        return {"view": view(out).model_dump()}

    def keep_for_work(self, repo: str, facts: WorkRepoFacts, actor: str) -> tuple[str, bool]:
        """Owner addition (2026-10-21): ``/work`` on a non-target repo keeps it (``source=owner`` on insert,
        ``decided_by`` the human). The caller ran ``hard_repo_check``; ``never`` refuses (400 ``never_repo``).
        Returns (canonical repo, newly kept). Runs inside the caller's transaction."""
        if self._is_target(repo):
            raise DiscoveredRefused(409, "is_target")
        if self._denied(repo):
            raise DiscoveredRefused(400, "denied")
        now = to_iso(self.clock.now())
        values = _work_facts(facts)
        with db.transaction(self.conn):
            row = self._row(repo)
            if row is not None and row["status"] == "never":
                raise DiscoveredRefused(400, "never_repo")
            if row is None:
                writer.insert_row(self.conn, {"repo": repo, "source": "owner", "status": "kept", **values,
                                              "decided_by": actor, "decided_at": now, "first_seen": now,
                                              "updated_at": now})  # fmt: skip
                key, before = repo, None
            else:
                key, before = str(row["repo"]), str(row["status"])
                writer.update_facts(self.conn, key, values, now)
                if before == "kept":
                    return key, False
                writer.set_status(self.conn, key, "kept", now, decided_by=actor, decided_at=now,
                                  cooldown_until=None, checked_at=None)  # fmt: skip
            self.audit.append(actor, "discovered_target_kept", {"repo": key, "from": before, "via": "work"})
            self.notify(f"⭐ Kept {key} as a target (open discovery, requested with /work): scanned every cycle.")
        return key, True

    def _untry_rows(self, since: str) -> builtins.list[str]:
        """Candidate rows tried at or after ``since`` whose repo got no contribution since then."""
        rows = self.conn.execute(
            "SELECT d.repo AS repo FROM discovered_targets d WHERE d.status = 'candidate' AND d.tried_at IS NOT NULL"
            " AND d.tried_at >= ? AND NOT EXISTS (SELECT 1 FROM contributions c WHERE c.upstream = d.repo"
            " COLLATE NOCASE AND c.created_at >= ?) ORDER BY d.repo",
            (since, since),
        )
        return [str(r["repo"]) for r in rows]

    def untry(self, since: str, apply: bool, actor: str) -> dict[str, Any]:
        """Discovery pacing (2026-10-22, fix 2), the owner's ``ossctl targets discovered --untry``: candidate
        rows tried since ``since`` (≤ 30 days back, not in the future; else 400 ``bad_since``) whose repo got
        no contribution since then lose ``tried_at`` and the cooldown, so open discovery takes them first.
        ``apply`` false previews; true writes, audits once and announces once (nothing when no row matches).
        Kept, never, skipped and suggested rows are never touched."""
        now_dt = self.clock.now()
        try:
            since_dt = from_iso(since)
        except ValueError as exc:
            raise DiscoveredRefused(400, "bad_since") from exc
        if since_dt > now_dt or now_dt - since_dt > timedelta(days=UNTRY_MAX_DAYS):
            raise DiscoveredRefused(400, "bad_since", f"within the last {UNTRY_MAX_DAYS} days")
        since_iso = to_iso(since_dt)
        if not apply:
            repos = self._untry_rows(since_iso)
            return {"since": since_iso, "repos": repos, "count": len(repos), "applied": False}
        now = to_iso(now_dt)
        with db.transaction(self.conn):
            repos = self._untry_rows(since_iso)
            for repo in repos:
                writer.clear_tried(self.conn, repo, now)
            if repos:
                self.audit.append(actor, "discovered_targets_untried", {"since": since_iso, "count": len(repos)})
                self.notify(f"↩️ {len(repos)} discovered repos are untried again (tried since {since_iso[:10]}, "
                            "no contribution); open discovery takes them first.")  # fmt: skip
        return {"since": since_iso, "repos": repos, "count": len(repos), "applied": True}

    def _announce(self, actor: str, event: str, repo: str, before: str | None, text: str) -> None:
        self.audit.append(actor, event, {"repo": repo, "from": before})
        self.notify(text)
