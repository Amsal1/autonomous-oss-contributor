"""Discover (DESIGN §9.4, §9.14): candidate issues from enabled targets, ranked, autonomous only.

Per enabled target with discovery on, no open PR of ours and no cooldown: open issues per configured
label, drop PRs, assigned/locked issues, ``skip_labels``, stale or crowded issues; rank with
``scoring`` (size fit first, filters from ``discovery_prefs``, the ``ci_on_pr_weight`` boost for a repo
whose CI ran on merged PRs, ``ci_detect`` cached per repo); check the top ones for an open linked
PR; create at most ``discovery.proposals_per_cycle`` contributions. The gatekeeper applies the
re-discovery rules (409 refusals are cached for one cycle). Reads only; nothing is posted. A cycle
reads for minutes, so a service stop (SIGTERM), kill, pause or drain ends it between targets.

Post-v1: a repo whose default branch has had no commit for ``max_default_branch_age_days`` is skipped
for the cycle (owner addition 2026-10-21; a recent commit is a small boost); kept discovered repos
join the seed loop (design §6.2); then, unless ``repos`` restricts the cycle, the open phase
(``open_discovery.OpenPhase``) adds tier-1 candidates, merged seed-first with ``reserved_slots``
(§7.3) and their mention checks paid from the open search budget.

A read that fails as ``github_http_<status>`` skips that target. A read that fails as
``github_unavailable`` (its retries used up) ends the cycle with ``GitHubUnavailable``: the next
target would wait through the same retries, and the scheduler tick that runs discovery dispatches
no stage until the cycle returns (F006-R4-1). The scheduler runs the cycle again later.

Discovery pacing (ADR-0003 addendum 2026-10-22): every seed, kept and open repo costs one ``GET
/repos/{r}/issues`` (per_page 100) with its labels OR-matched on the client (``od.has_any_label``), so
``GET /search/issues`` is used only for the mention checks. While GitHub reports fewer than
``CORE_FLOOR`` core requests left, the seed and kept loops stop, the open phase and CI reads are
skipped and the cycle finishes with what it read (INFO ``discover_core_floor``). Discovery's searches
wait out a search limit of ≤ 65 s; a longer one ends only the open phase. A cycle cut short keeps its
progress in a ``CycleProgress`` (the scheduler's), and the re-run skips the targets already read and
an open phase that finished; the merge runs again with every pick's mention check. Open repos are
recorded ``tried`` only after a merge that ended normally (``od.mark_tried``).

Visibility (discovery fix 5a): ``CycleProgress.stats`` (a ``CycleStats``) counts what the cycle read,
ranked, dropped, was refused and created, across its resumes; a finished run logs INFO
``discover_done`` with every count, and ``cycle_report`` builds the body the scheduler sends to the
gatekeeper (``POST /v1/discovery-cycles``) for every finished or cut cycle.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from ossbot.common.clock import from_iso, to_iso
from ossbot.common.config import (
    ConfigBundle,
    DiscoveryFilters,
    ResolvedTarget,
    all_targets,
    discovered_target,
    resolve_target,
)
from ossbot.runner import ci_detect, scoring
from ossbot.runner import open_discovery as od
from ossbot.runner.errors import Deferred
from ossbot.runner.github_read import UNAVAILABLE, GitHubReadError
from ossbot.runner.gk_client import GkError
from ossbot.runner.pipeline import Interrupted, StageContext

log = logging.getLogger(__name__)
STAGE = "discover"
ACTIVE_PR_STATES = frozenset({"AWAITING_APPROVAL", "APPROVED", "SUBMITTING", "SUBMITTED", "WATCHING"})
CORE_FLOOR = 1000  # x-ratelimit-remaining (core) below this: no more seed/kept/open/CI reads this cycle


class GitHubUnavailable(Deferred):
    """GitHub's API stayed unavailable through a read's retries: the cycle ends and runs again later."""


REASON_CODE_RE = re.compile(r"^[a-z0-9_:.-]{1,64}$")  # schemas.DiscoveryCycleReport drops keys
DROPS_REPORTED = 12  # the top drop reasons a cycle report carries
CREATED_REPORTED = 10  # the cids a cycle report carries
COUNT_MAX = 100_000


@dataclass
class CycleStats:
    """What one discovery cycle did (discovery fix 5a): logged as INFO ``discover_done`` when the cycle
    finishes and sent to the gatekeeper as a cycle report (``POST /v1/discovery-cycles``). Read and merge
    counts accumulate across the resumes of a cut cycle; the ranking counts and ``drops`` are those of the
    last ranking."""

    targets_read: int = 0  # seed targets whose issues were read
    kept_read: int = 0  # kept discovered repos whose issues were read
    inactive: int = 0  # repos skipped by the default-branch activity gate
    open_repos_scanned: int = 0
    candidates: int = 0  # seed and kept candidates after keep_issue
    open_candidates: int = 0
    ranked: int = 0  # tier 0 after the filters
    open_ranked: int = 0
    drops: Counter[str] = field(default_factory=Counter)  # scoring.rank drop reasons, both tiers
    refused: int = 0  # gatekeeper 400/409 (not_rediscoverable, duplicate_active)
    linked_pr: int = 0  # an open PR mentions the issue
    created_total: int = 0  # contributions this cycle created (all attempts)
    resumes: int = 0
    core_floor: bool = False
    open_ended: str | None = None  # "rate_limited" | "http_error"

    def counts(self) -> dict[str, Any]:
        out: dict[str, Any] = {k: min(int(v), COUNT_MAX) for k, v in vars(self).items()
                               if isinstance(v, int) and not isinstance(v, bool)}  # fmt: skip
        out["core_floor"] = self.core_floor
        out["open_ended"] = self.open_ended
        out["drops"] = {r: min(n, COUNT_MAX) for r, n in self.drops.most_common() if REASON_CODE_RE.fullmatch(r)}
        return out


@dataclass
class CycleProgress:
    """What a full cycle has read so far (the scheduler keeps it across a cut, in memory only).

    ``done`` holds the lower-cased seed and kept repos fully handled (read, archived, inactive, never or
    skipped on a 4xx); ``seed_finished`` is set after the kept loop or at the core floor; ``open_done``
    once the open phase ran without ending on a search limit (or is off)."""

    started_at: datetime
    done: set[str] = field(default_factory=set)
    candidates: list[scoring.Candidate] = field(default_factory=list)
    hashes: dict[str, str] = field(default_factory=dict)
    per_repo_max: dict[str, str] = field(default_factory=dict)
    seed_finished: bool = False
    floor: bool = False
    opened: od.OpenResult | None = None
    open_done: bool = False
    reached: set[str] = field(default_factory=set)  # open repos a merge attempt got to (od.mark_tried)
    stats: CycleStats = field(default_factory=CycleStats)


def cycle_report(progress: CycleProgress, *, outcome: Literal["finished", "cut"], scope: Literal["full", "repos"],
                 cut_reason: str | None, created: list[str], ended_at: datetime) -> dict[str, Any]:  # fmt: skip
    """The body of ``POST /v1/discovery-cycles`` (``schemas.DiscoveryCycleReport``): counts only, the top
    drop reasons and the first cids created; runner data, never shown before the gatekeeper validated it."""
    progress.stats.created_total = len(created)
    counts = progress.stats.counts()
    drops = dict(list(counts.pop("drops").items())[:DROPS_REPORTED])
    return {"outcome": outcome, "scope": scope, "cut_reason": cut_reason if outcome == "cut" else None,
            "started_at": to_iso(progress.started_at), "ended_at": to_iso(ended_at),
            "created": created[:CREATED_REPORTED], "drops": drops, **counts}  # fmt: skip


def _core_low(sc: StageContext, progress: CycleProgress) -> bool:
    """True (and remembered for the cycle) once GitHub reports fewer than ``CORE_FLOOR`` core requests."""
    remaining = sc.gh.core_left()
    if not progress.floor and remaining is not None and remaining < CORE_FLOOR:
        progress.floor = True
        log.info("discovery stops reading: %d core requests left (reset %s); finishing with what was read",
                 remaining, sc.gh.core_reset,
                 extra={"event": "discover_core_floor", "remaining": remaining, "reset": sc.gh.core_reset})  # fmt: skip
    return progress.floor


def _end_if_unavailable(exc: GitHubReadError) -> None:
    if exc.error_class == UNAVAILABLE:
        raise GitHubUnavailable(str(exc)) from exc


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


def _check(sc: StageContext) -> None:
    if sc.stopping():
        raise Interrupted("runner stopping")
    sc.gate.check(STAGE)


def _rank_open(opened: od.OpenResult | None, *, prefs: dict[str, Any], filters: DiscoveryFilters, now: datetime,
               bundle: ConfigBundle, drops: Counter[str]) -> list[scoring.Ranked]:  # fmt: skip
    """Tier 1 (§7.3, review-3 L3): ranked with ``signals={}`` and ``ci_weight=0``; the repo's CI boost
    is already in ``Candidate.repo_bonus``, so it counts once. The global autonomous class applies.
    Drop reasons are added to ``drops``."""
    if opened is None or not opened.candidates:
        return []
    kept, dropped = scoring.rank(opened.candidates, filters=filters, max_class=str(prefs["autonomous_max_class"]),
                                 preferred=list(prefs["preferred_classes"]), signals={}, now=now, ci_weight=0.0,
                                 interests=bundle.profile.interests.categories_priority)  # fmt: skip
    drops.update(dropped.values())
    return kept


def _merge(tier0: list[scoring.Ranked], tier1: list[scoring.Ranked], remaining: int,
           reserved_slots: int) -> list[tuple[scoring.Ranked, int]]:  # fmt: skip
    """§7.3 creation order: the top ``remaining - reserved`` seed/kept candidates, then the open ones,
    then the rest of the seed/kept ones (``reserved = min(reserved_slots, remaining)`` if any open)."""
    reserved = min(reserved_slots, max(0, remaining)) if tier1 else 0
    head = max(0, remaining - reserved)
    return [(r, 0) for r in tier0[:head]] + [(r, 1) for r in tier1] + [(r, 0) for r in tier0[head:]]


def _scan_issues(sc: StageContext, target: ResolvedTarget, info: dict[str, Any], now: datetime, bonus: float,
                 candidates: list[scoring.Candidate], hashes: dict[str, str]) -> None:  # fmt: skip
    """One ``GET /repos/{r}/issues`` (core, per_page 100) for a seed or kept repo; the target's labels are
    OR-matched on the client, case-insensitively (a target without labels takes every open issue)."""
    seen: set[int] = set()
    skip = {s.lower() for s in target.skip_labels}
    for issue in sc.gh.issues(target.repo):
        number = int(issue.get("number", 0))
        if number in seen or not od.has_any_label(issue, target.labels) or not keep_issue(issue, skip, now):
            continue
        seen.add(number)
        hashes[f"{target.repo}#{number}"] = issue_hash(issue)
        candidates.append(scoring.candidate_from_issue(
            target.repo, issue, priority=target.priority, category=target.category,
            language=info.get("language"), repo_bonus=bonus))  # fmt: skip


def _activity(sc: StageContext, repo: str, info: dict[str, Any], now: datetime, max_days: int) -> float | None:
    """Owner addition (2026-10-21): the default branch's last commit, one read per repo per cycle.
    None = skip the repo this cycle (``inactive_default_branch``, INFO; re-checked next cycle, never
    removed); else the small activity boost. An unknown date is not a skip."""
    branch = info.get("default_branch")
    if not isinstance(branch, str) or not branch:
        return 0.0
    commit_at = od.branch_commit_at(sc.gh, repo, branch)
    if od.inactive_reason(commit_at, now, max_days):
        log.info("discovery skips %s this cycle: no commit on %s since %s", repo, branch, commit_at,
                 extra={"event": "discover_repo_inactive", "repo": repo, "reason": od.INACTIVE})  # fmt: skip
        return None
    return od.branch_activity_bonus(commit_at, now)


def _egress_mode(sc: StageContext) -> str | None:
    """The active egress mode for rule 14; a failed read is unknown, which counts as strict (§7.4)."""
    try:
        return sc.gk.egress_mode()
    except GkError as exc:
        log.warning("egress mode unknown (%s); open discovery treats every repo as strict", exc.code,
                    extra={"event": "egress_mode_unknown"})  # fmt: skip
        return None


def _kept_due(row: dict[str, Any], now: datetime, cooldown_days: int) -> bool:
    checked = row.get("checked_at")
    if not checked:
        return True
    try:
        return now - from_iso(str(checked)) > timedelta(days=cooldown_days)
    except ValueError:
        return True


def _kept_check(sc: StageContext, row: dict[str, Any], info: dict[str, Any] | None, now: datetime,
                egress_mode: str | None) -> bool:  # fmt: skip
    """§6.2: the re-check of a kept repo (``GET /repos`` already read): rules 3-5, 10-12 and 14 when its
    egress is strict; the result goes back with ``kept_check: true``. True = scan it."""
    bundle = sc.config()
    repo = str(row["repo"])
    hit = od.RepoHit.from_item(info) if info is not None else None
    reason = od.NOT_FOUND if hit is None else od.kept_reason(hit)
    repo_strict = bool(row.get("egress_strict")) or bundle.targets.defaults.egress == "strict"
    if hit is not None and reason is None and od.effective_strict(egress_mode, "strict" if repo_strict else "mode"):
        hosts = od.uncovered_hosts(od.egress_hosts(od.precheck_files(sc.gh, repo)), od.allowed_hosts(bundle))
        if hosts:
            reason = od.egress_reason(hosts, repo_strict=repo_strict)
    status = "skipped" if reason else "candidate"
    body = (od.hit_facts(hit, source="search", status=status, skip_reason=reason, kept_check=True,
                         topic=row.get("topic"), ci_on_pr=row.get("ci_on_pr"), score=row.get("repo_score"))
            if hit is not None else
            od.facts_body(repo, source="search", status=status, skip_reason=reason, kept_check=True))  # fmt: skip
    od.record(sc, body)
    return reason is None


def _kept_one(sc: StageContext, row: dict[str, Any], *, now: datetime, egress_mode: str | None,
              progress: CycleProgress) -> None:  # fmt: skip
    """One kept repo (§6.2): the re-check when due, then the scan like a seed repo."""
    bundle = sc.config()
    disc = bundle.policy.discovery
    repo = str(row["repo"])
    due = _kept_due(row, now, disc.open.repo_cooldown_days)
    if not due and row.get("skip_reason"):
        return  # not scanned until a later re-check clears it
    repo_prefs = sc.gk.prefs(repo)
    if repo_prefs.get("never_repo"):
        return
    target = discovered_target(bundle.targets, repo, kept=True, egress_strict=bool(row.get("egress_strict")))
    if target is None:
        return
    try:
        info = sc.gh.repo(repo)
        if (due and not _kept_check(sc, row, info, now, egress_mode)) or info is None:
            return
        bonus = _activity(sc, repo, info, now, disc.max_default_branch_age_days)
        if bonus is None:
            progress.stats.inactive += 1
            return
        progress.per_repo_max[repo] = str(repo_prefs["autonomous_max_class"])
        _scan_issues(sc, target, info, now, bonus, progress.candidates, progress.hashes)
        progress.stats.kept_read += 1
    except GitHubReadError as exc:
        log.warning("discovery read failed for %s: %s", repo, exc, extra={"event": "discover_read_failed"})
        _end_if_unavailable(exc)


def _kept(sc: StageContext, *, repos: list[str] | None, active: set[str], now: datetime, egress_mode: str | None,
          progress: CycleProgress) -> None:  # fmt: skip
    """§6.2: kept discovered repos join the seed loop after the config targets (first_seen order),
    scanned like a seed repo with ``KEPT_PRIORITY`` and no cooldown, re-checked when due. Repos in
    ``progress.done`` are not read again; the core floor stops the loop."""
    bundle = sc.config()
    try:
        rows = sc.gk.discovered("kept", limit=500)
    except GkError as exc:
        log.warning("kept repos skipped this cycle: gatekeeper answered %s %s", exc.status, exc.code,
                    extra={"event": "open_phase_skipped"})  # fmt: skip
        return
    wanted = {r.lower() for r in repos or ()}
    for row in sorted(rows, key=lambda r: str(r.get("first_seen") or "")):
        repo = str(row.get("repo") or "")
        key = repo.lower()
        if not repo or (wanted and key not in wanted) or resolve_target(bundle.targets, repo) is not None:
            continue
        if key in progress.done:
            continue  # read before a cut: its candidates are carried
        _check(sc)
        if key in active:
            continue  # M2: one open PR per repo, as for seed repos
        if _core_low(sc, progress):
            return
        _kept_one(sc, row, now=now, egress_mode=egress_mode, progress=progress)
        progress.done.add(key)


def _open_phase(sc: StageContext, *, prefs: dict[str, Any], active: set[str], now: datetime,
                egress_mode: str | None) -> tuple[od.OpenResult | None, bool]:  # fmt: skip
    """§7.1 step 3, only when the effective ``open.enabled`` holds and the search bucket is not waiting.
    Returns (result, done): ``done`` is False while the phase still has to run in this cycle (the search
    bucket is waiting, or the phase ended on a search limit), so a re-run of a cut cycle runs it again."""
    bundle = sc.config()
    open_prefs = prefs.get("open") if isinstance(prefs.get("open"), dict) else None
    enabled = bundle.policy.discovery.open.enabled
    if open_prefs and "enabled" in open_prefs:
        enabled = bool(open_prefs["enabled"])  # /filters page "Open" (a preference, global scope only)
    if not enabled:
        return None, True
    until = sc.load(od.OPEN_NOTE).get("search_until")
    if isinstance(until, int | float) and now.timestamp() < until:
        log.info("open discovery waits for the search rate limit", extra={"event": "open_discovery_waiting"})
        return None, False
    phase = od.OpenPhase(sc, bundle=bundle, prefs_open=open_prefs, active=active, egress_mode=egress_mode, now=now,
                         check=lambda: _check(sc))  # fmt: skip
    try:
        result = phase.run()
    except GitHubReadError as exc:  # only github_unavailable leaves the phase
        _end_if_unavailable(exc)
        raise
    return result, result.ended != "rate_limited"


def _seed_one(sc: StageContext, entry: ResolvedTarget, *, now: datetime, progress: CycleProgress) -> None:
    """One config target: ``GET /repos``, the default-branch activity gate, one issues read."""
    repo_prefs = sc.gk.prefs(entry.repo)
    if repo_prefs.get("never_repo"):
        return
    progress.per_repo_max[entry.repo] = str(repo_prefs["autonomous_max_class"])
    try:
        info = sc.gh.repo(entry.repo) or {}
        if info.get("archived"):
            return
        bonus = _activity(sc, entry.repo, info, now, sc.config().policy.discovery.max_default_branch_age_days)
        if bonus is None:
            progress.stats.inactive += 1
            return  # skipped this cycle, kept in targets.yaml
        _scan_issues(sc, entry, info, now, bonus, progress.candidates, progress.hashes)
        progress.stats.targets_read += 1
    except GitHubReadError as exc:
        log.warning("discovery read failed for %s: %s", entry.repo, exc, extra={"event": "discover_read_failed"})
        _end_if_unavailable(exc)


def _seed(sc: StageContext, *, repos: list[str] | None, active: set[str], now: datetime, egress_mode: str | None,
          progress: CycleProgress) -> None:  # fmt: skip
    """The seed loop (config targets), then the kept repos; targets in ``progress.done`` are skipped.
    ``seed_finished`` is set when both loops ended or the core floor stopped them."""
    for entry in all_targets(sc.config().targets):
        if not entry.enabled or not entry.discovery or (repos and entry.repo not in repos):
            continue
        key = entry.repo.lower()
        if key in progress.done:
            continue  # read before a cut: its candidates are carried
        _check(sc)  # a cycle reads for minutes: a stop, kill, pause or drain ends it between targets
        if key in active:
            continue  # max_open_prs_by_me_per_repo (default 1) open PRs there already
        if _core_low(sc, progress):
            break
        _seed_one(sc, entry, now=now, progress=progress)
        progress.done.add(key)
    if not progress.floor:
        _kept(sc, repos=repos, active=active, now=now, egress_mode=egress_mode, progress=progress)
    progress.seed_finished = True


def _rank_seed(sc: StageContext, progress: CycleProgress, *, prefs: dict[str, Any], filters: DiscoveryFilters,
               active: set[str], now: datetime, drops: Counter[str]) -> list[scoring.Ranked]:  # fmt: skip
    """Tier 0: the carried seed and kept candidates, ranked with the repo's CI facts (cached; not read
    at the core floor, where the status is ``unknown``). The drop reasons of each repo's final ranking
    are added to ``drops``."""
    bundle = sc.config()
    disc = bundle.policy.discovery
    by_repo: dict[str, list[scoring.Candidate]] = {}
    for cand in progress.candidates:
        if cand.repo.lower() not in active:  # an open PR of ours appeared since a cut: not this repo
            by_repo.setdefault(cand.repo, []).append(cand)
    ranked: list[scoring.Ranked] = []
    for repo, cands in by_repo.items():
        rank_args: dict[str, Any] = {"filters": filters, "max_class": progress.per_repo_max[repo], "now": now,
                                     "preferred": list(prefs["preferred_classes"]),
                                     "interests": bundle.profile.interests.categories_priority}  # fmt: skip
        kept, dropped = scoring.rank(cands, signals={}, **rank_args)
        if not kept:
            drops.update(dropped.values())
            continue
        _check(sc)  # CI detection reads (cached per repo): a stop, kill, pause or drain ends the cycle here
        ci: Literal["yes", "no", "unknown"] = "unknown"
        if not _core_low(sc, progress):
            try:
                ci = ci_detect.cached(sc, repo, disc.ci_facts_ttl_hours).status
            except GitHubReadError as exc:
                log.warning("CI detection failed for %s: %s", repo, exc, extra={"event": "discover_ci_failed"})
                _end_if_unavailable(exc)
        kept, dropped = scoring.rank(cands, signals={repo: scoring.RepoSignals(ci_on_pr=ci)},
                                     ci_weight=disc.ci_on_pr_weight, **rank_args)  # fmt: skip
        drops.update(dropped.values())
        ranked += kept
    ranked.sort(key=lambda r: -r.score)
    return ranked


def run(sc: StageContext, *, repos: list[str] | None = None, now: datetime | None = None,
        refused: set[str] | None = None, created: list[str] | None = None,
        progress: CycleProgress | None = None) -> list[str]:  # fmt: skip
    """One discovery cycle; returns the cids created by this call. ``repos`` limits it (``ossctl
    discover``; no open phase then). ``created`` carries the cids an earlier, cut-short attempt of the
    same cycle created: they count against ``proposals_per_cycle`` and new ones are appended to it.
    ``progress`` (full cycles, kept by the scheduler) carries what that attempt read: finished targets
    are not read again, a finished open phase is not run again, and the merge runs on the carried
    candidates with a fresh mention check per pick."""
    sc.gate.check(STAGE)
    now = now or datetime.now(UTC)
    bundle = sc.config()
    refused = refused if refused is not None else set()
    progress = progress if progress is not None else CycleProgress(now)
    open_prs: dict[str, int] = {}
    for open_c in sc.gk.active_contributions():
        if open_c.get("state") in ACTIVE_PR_STATES:
            repo_key = str(open_c["upstream"]).lower()
            open_prs[repo_key] = open_prs.get(repo_key, 0) + 1
    prefs = sc.gk.prefs()
    filters = DiscoveryFilters.model_validate(prefs["filters"])
    active = {r for r, n in open_prs.items() if n >= filters.max_open_prs_by_me_per_repo}
    egress_mode = _egress_mode(sc)
    disc = bundle.policy.discovery
    if not progress.seed_finished:
        _seed(sc, repos=repos, active=active, now=now, egress_mode=egress_mode, progress=progress)
    if repos or _core_low(sc, progress):
        progress.open_done = True  # no open phase for ossctl discover, nor at the core floor
    if not progress.open_done:
        # a re-run replaces an open phase that ended on a search limit; the candidate rows it recorded
        # are untried, so the new phase takes them first (carry-over)
        progress.opened, progress.open_done = _open_phase(sc, prefs=prefs, active=active, now=now,
                                                          egress_mode=egress_mode)  # fmt: skip
    opened = progress.opened
    created = created if created is not None else []
    already = len(created)
    budget = disc.proposals_per_cycle
    stats = progress.stats
    stats.drops = Counter()  # the drops of this (last) ranking
    ranked = _rank_seed(sc, progress, prefs=prefs, filters=filters, active=active, now=now, drops=stats.drops)
    tier1 = _rank_open(opened, prefs=prefs, filters=filters, now=now, bundle=bundle, drops=stats.drops)
    stats.candidates, stats.ranked, stats.open_ranked = len(progress.candidates), len(ranked), len(tier1)
    stats.core_floor = progress.floor
    if opened is not None:
        stats.open_repos_scanned, stats.open_candidates = len(opened.scanned), len(opened.candidates)
        stats.open_ended = opened.ended
    order = _merge(ranked, tier1, budget - len(created), disc.open.reserved_slots)
    sources = opened.sources if opened is not None else {}
    hashes = {**progress.hashes, **(opened.hashes if opened is not None else {})}
    reached = progress.reached  # open repos whose tier-1 pick a merge attempt got to (od.mark_tried)
    for item, tier in order:
        if len(created) >= budget:
            break
        _check(sc)
        repo, number = item.candidate.repo, item.candidate.number
        key = f"{repo}#{number}"
        if tier == 1:
            reached.add(repo.lower())
        if key in refused:
            continue
        if tier == 1 and opened is not None and not opened.budget.take():
            continue  # §7.3: an unpaid tier-1 mention check skips the pick (not cached); tier 0 takes the slot
        try:
            # no contribution without its mention check; a search limit of ≤ 65 s is waited out here,
            # a longer one cuts the cycle (the scheduler keeps the progress and re-runs the merge)
            if sc.gh.open_prs_mentioning(repo, number, check=lambda: _check(sc)) > 0:
                refused.add(key)
                stats.linked_pr += 1
                continue
            created.append(sc.gk.create(repo, number, sources.get(key, "discover"), round(item.score, 2),
                                        hashes.get(key)))  # fmt: skip
        except GkError as exc:
            if exc.status in {400, 409}:
                refused.add(key)  # not_rediscoverable / duplicate_active: cached for this cycle
                stats.refused += 1
                continue
            raise
        except GitHubReadError as exc:
            _end_if_unavailable(exc)
            continue
    if opened is not None:  # the merge ended normally: record which open repos it used (fix 2)
        od.mark_tried(sc, opened, reached=reached, ranked={r.candidate.repo.lower() for r in tier1})
    stats.created_total = len(created)
    counts = stats.counts()
    log.info("discovery cycle done: %d candidates (%d open), %d ranked (%d open), %d created, %d refused,"
             " %d with a linked PR", stats.candidates, stats.open_candidates, stats.ranked, stats.open_ranked,
             stats.created_total, stats.refused, stats.linked_pr,
             extra={"event": "discover_done", "scope": "repos" if repos else "full", "counts": counts})  # fmt: skip
    return created[already:]
