"""Discover (DESIGN §9.4, §9.14): candidate issues from enabled targets, ranked, autonomous only.

Per enabled target with discovery on, no open PR of ours and no cooldown: open issues per configured
label, drop PRs, assigned/locked issues, ``skip_labels``, stale or crowded issues; rank with
``scoring`` (size fit first, filters from ``discovery_prefs``); check the top ones for an open linked
PR; create at most ``discovery.proposals_per_cycle`` contributions. The gatekeeper applies the
re-discovery rules (409 refusals are cached for one cycle). Reads only; nothing is posted.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from ossbot.common.config import DiscoveryFilters, all_targets
from ossbot.runner import scoring
from ossbot.runner.github_read import GitHubReadError
from ossbot.runner.gk_client import GkError
from ossbot.runner.pipeline import StageContext

log = logging.getLogger(__name__)
STAGE = "discover"
ACTIVE_PR_STATES = frozenset({"AWAITING_APPROVAL", "APPROVED", "SUBMITTING", "SUBMITTED", "WATCHING"})


def issue_hash(issue: dict[str, Any]) -> str:
    labels = sorted(str(lbl.get("name", "")) for lbl in issue.get("labels") or [] if isinstance(lbl, dict))
    doc = {"title": issue.get("title") or "", "body": issue.get("body") or "", "labels": labels}
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode("utf-8")).hexdigest()


def keep_issue(issue: dict[str, Any], skip_labels: set[str], now: datetime) -> bool:
    if "pull_request" in issue or issue.get("assignee") or issue.get("assignees") or issue.get("locked"):
        return False
    labels = {str(lbl.get("name", "")).lower() for lbl in issue.get("labels") or [] if isinstance(lbl, dict)}
    if labels & skip_labels or int(issue.get("comments") or 0) > 30:
        return False
    from ossbot.common.clock import from_iso  # noqa: PLC0415

    return (now - from_iso(str(issue["updated_at"]))).days <= 180


def run(sc: StageContext, *, repos: list[str] | None = None, now: datetime | None = None,
        refused: set[str] | None = None) -> list[str]:  # fmt: skip
    """One discovery cycle; returns the created cids. ``repos`` limits it (``ossctl discover``)."""
    sc.gate.check(STAGE)
    now = now or datetime.now(UTC)
    bundle = sc.config()
    refused = refused if refused is not None else set()
    active = {str(c["upstream"]).lower() for c in sc.gk.contributions(limit=200)
              if c.get("state") in ACTIVE_PR_STATES}  # fmt: skip
    prefs = sc.gk.prefs()
    filters = DiscoveryFilters.model_validate(prefs["filters"])
    candidates: list[scoring.Candidate] = []
    hashes: dict[str, str] = {}
    per_repo_max: dict[str, str] = {}
    for entry in all_targets(bundle.targets):
        if not entry.enabled or not entry.discovery or (repos and entry.repo not in repos):
            continue
        if entry.repo.lower() in active:
            continue  # one open PR per repo for autonomous work
        repo_prefs = sc.gk.prefs(entry.repo)
        if repo_prefs.get("never_repo"):
            continue
        per_repo_max[entry.repo] = str(repo_prefs["autonomous_max_class"])
        seen: set[int] = set()
        try:
            info = sc.gh.repo(entry.repo) or {}
            if info.get("archived"):
                continue
            for label in entry.labels or ("",):
                for issue in sc.gh.issues(entry.repo, label or None):
                    number = int(issue.get("number", 0))
                    if number in seen or not keep_issue(issue, {s.lower() for s in entry.skip_labels}, now):
                        continue
                    seen.add(number)
                    hashes[f"{entry.repo}#{number}"] = issue_hash(issue)
                    candidates.append(scoring.candidate_from_issue(
                        entry.repo, issue, priority=entry.priority, category=entry.category,
                        language=info.get("language")))  # fmt: skip
        except GitHubReadError as exc:
            log.warning("discovery read failed for %s: %s", entry.repo, exc, extra={"event": "discover_read_failed"})
    created: list[str] = []
    budget = bundle.policy.discovery.proposals_per_cycle
    by_repo: dict[str, list[scoring.Candidate]] = {}
    for cand in candidates:
        by_repo.setdefault(cand.repo, []).append(cand)
    ranked: list[scoring.Ranked] = []
    for repo, cands in by_repo.items():
        kept, _dropped = scoring.rank(cands, filters=filters, max_class=per_repo_max[repo],
                                      preferred=list(prefs["preferred_classes"]), signals={}, now=now,
                                      interests=bundle.profile.interests.categories_priority)  # fmt: skip
        ranked += kept
    ranked.sort(key=lambda r: -r.score)
    for item in ranked:
        if len(created) >= budget:
            break
        key = f"{item.candidate.repo}#{item.candidate.number}"
        if key in refused:
            continue
        try:
            if sc.gh.open_prs_mentioning(item.candidate.repo, item.candidate.number) > 0:
                refused.add(key)
                continue
            created.append(sc.gk.create(item.candidate.repo, item.candidate.number, "discover",
                                        round(item.score, 2), hashes.get(key)))  # fmt: skip
        except GkError as exc:
            if exc.status in {400, 409}:
                refused.add(key)  # not_rediscoverable / duplicate_active: cached for this cycle
                continue
            raise
        except GitHubReadError:
            continue
    return created
