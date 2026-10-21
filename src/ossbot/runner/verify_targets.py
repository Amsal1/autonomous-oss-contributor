"""``ossctl targets verify`` worker side (DESIGN §17): read every enabled target with the read token.

Runs as ``ossrunner`` (``ossctl`` starts it with ``systemd-run --uid=ossrunner`` and
``EnvironmentFile=runner.env``), so ``GITHUB_READ_TOKEN`` never enters the operator's process and
the request leaves through proxy 3140 like every runner read. Input on stdin: ``{"repos": [...],
"stale_days": N}``, or ``{"mode": "facts", "repos": [...]}`` (≤ 5) for ``ossctl targets facts``, or
``{"mode": "search", "topics", "min_stars", …}`` for the open-discovery dry run ``ossctl targets
search`` (design §10.2; exits 1 on a rate limit). Every mode may carry
``max_default_branch_age_days`` (the default-branch activity gate, owner addition 2026-10-21).
Output on stdout: one JSON object ``{"rows": [...], "ok": bool}``. GET only; the token value is never
printed.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from ossbot.common import ai_policy, ci_facts
from ossbot.common.clock import from_iso
from ossbot.runner import ci_detect
from ossbot.runner import open_discovery as od
from ossbot.runner.github_read import UNAVAILABLE, GitHubRateLimited, GitHubRead, GitHubReadError
from ossbot.runner.repo_docs import fetch_policy_docs

FACTS_MAX = 5
SEARCH_TOPICS_MAX = 25
SEARCH_TOP_MAX = 25
BRANCH_AGE_DEFAULT = 180  # policy.discovery.max_default_branch_age_days when the request has none


def _branch_age(gh: GitHubRead, repo: str, info: dict[str, Any], now: datetime) -> tuple[str | None, int | None]:
    """Owner addition (2026-10-21): the default branch's last commit and its age in days (None = unknown)."""
    branch = info.get("default_branch")
    if not isinstance(branch, str) or not branch:
        return None, None
    try:
        commit_at = od.branch_commit_at(gh, repo, branch)
        return commit_at, (now - from_iso(commit_at)).days if commit_at else None
    except (GitHubReadError, GitHubRateLimited, ValueError):
        return None, None  # unknown: never a skip


def verify(gh: GitHubRead, repos: list[str], stale_days: int, now: datetime,
           max_branch_days: int = BRANCH_AGE_DEFAULT) -> list[dict[str, Any]]:  # fmt: skip
    """One row per repo. Once a read fails as ``github_unavailable`` (its retries used up), the
    rest are FAIL without a read, so an outage costs one read's retries, not one per target. A
    default branch without a commit for ``max_branch_days`` is a WARN ``inactive_default_branch``
    (discovery skips the target each cycle until it is active again; it stays in targets.yaml)."""
    rows: list[dict[str, Any]] = []
    unavailable: str | None = None
    for repo in repos:
        row: dict[str, Any] = {"repo": repo}
        if unavailable is not None:
            rows.append({**row, "status": "FAIL", "detail": f"not checked: {unavailable}"[:120]})
            continue
        try:
            info = gh.repo(repo)
        except (GitHubReadError, GitHubRateLimited) as exc:
            rows.append({**row, "status": "FAIL", "detail": str(exc)[:120]})
            if isinstance(exc, GitHubReadError) and exc.error_class == UNAVAILABLE:
                unavailable = UNAVAILABLE
            continue
        if info is None:
            rows.append({**row, "status": "FAIL", "detail": "not found (or not readable with the read token)"})
            continue
        pushed = info.get("pushed_at")
        age = (now - from_iso(str(pushed))).days if pushed else None
        row.update(pushed_at=pushed, stars=info.get("stargazers_count"), default_branch=info.get("default_branch"))
        commit_at, branch_days = (None, None) if info.get("archived") else _branch_age(gh, repo, info, now)
        row["default_branch_commit_at"] = commit_at
        if info.get("archived"):
            row.update(status="FAIL", detail="archived")
        elif info.get("full_name") and str(info["full_name"]).lower() != repo.lower():
            row.update(status="WARN", detail=f"renamed to {info['full_name']}")
        elif branch_days is not None and branch_days > max_branch_days:
            row.update(status="WARN", detail=f"{od.INACTIVE}: no commit on {info.get('default_branch')} for "
                                             f"{branch_days} days (discovery skips it this cycle)")  # fmt: skip
        elif age is not None and age > stale_days:
            row.update(status="WARN", detail=f"no push for {age} days")
        else:
            row.update(status="PASS", detail="")
        rows.append(row)
    return rows


def facts(gh: GitHubRead, repos: list[str], now: datetime | None = None,
          max_branch_days: int = BRANCH_AGE_DEFAULT) -> list[dict[str, Any]]:  # fmt: skip
    """``ossctl targets facts``: the deterministic AI-policy classification and the CI-on-PR facts per
    repo (≤ ``FACTS_MAX``), the same code vet and discovery use, and the default branch's last commit
    (``inactive`` past ``max_branch_days``). No LLM, no gatekeeper call, no row."""
    now = now or datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for repo in repos[:FACTS_MAX]:
        row: dict[str, Any] = {"repo": repo}
        try:
            info = gh.repo(repo)
            if info is None:
                rows.append({**row, "status": "FAIL", "detail": "not found (or not readable with the read token)"})
                continue
            commit_at, branch_days = _branch_age(gh, repo, info, now)
            inactive = branch_days is not None and branch_days > max_branch_days
            row.update(default_branch=info.get("default_branch"), default_branch_commit_at=commit_at,
                       branch_days=branch_days, inactive=inactive)  # fmt: skip
            docs = fetch_policy_docs(gh, repo)
            cls = ai_policy.classify(docs.values())
            contributing = docs["contributing"].text if "contributing" in docs else ""
            ci = ci_detect.detect(gh, repo, contributing=contributing)
        except (GitHubReadError, GitHubRateLimited) as exc:
            rows.append({**row, "status": "FAIL", "detail": str(exc)[:120]})
            continue
        line = cls.ban_line if cls.status == "banned" else cls.allow_line
        rows.append({**row, "status": "PASS", "detail": "", "ai_policy": cls.status,
                     "ai_display": ai_policy.display(cls.status, cls.conditions), "ai_source": cls.source,
                     "ai_line": " ".join(line.split())[:80], "conditions": [c.kind for c in cls.conditions],
                     "ci_display": ci_facts.display(ci), **ci.stored()})  # fmt: skip
    return rows


def _strs(value: Any, limit: int) -> list[str]:
    return [str(v) for v in value][:limit] if isinstance(value, list) else []


def search(gh: GitHubRead, req: dict[str, Any], now: datetime) -> dict[str, Any]:
    """``ossctl targets search`` (design §10.2): one repo search per topic (≤ 25, at the 2.1 s gap),
    rules 1-12, the activity gate and rule 13 on the top ``2 N`` (rule 14 only reported, "would skip
    if strict"), ``ci_detect.detect`` (no runner note) on the top ``N + 2``, the final rank.

    GET only; no gatekeeper call, no ``sc.save``, no Telegram message, no row. A rate limit answers
    ``{"ok": false, "error": "rate_limited", "until": …}``."""
    top = max(1, min(SEARCH_TOP_MAX, int(req.get("top") or 10)))
    min_stars = int(req.get("min_stars") or 1000)
    pushed_days = int(req.get("pushed_within_days") or 60)
    languages = _strs(req.get("languages_preferred"), 20)
    language_weight = float(req.get("language_weight") or 0.0)
    ci_weight = float(req.get("ci_weight") or 0.0)
    max_branch_days = int(req.get("max_default_branch_age_days") or BRANCH_AGE_DEFAULT)
    rules = od.RepoRules(config_repos=frozenset(r.lower() for r in _strs(req.get("exclude"), 5000)),
                         deny_patterns=tuple(_strs(req.get("deny_patterns"), 500)),
                         own_logins=frozenset(x.lower() for x in _strs(req.get("own_logins"), 50)),
                         min_stars=min_stars, pushed_within_days=pushed_days)  # fmt: skip
    allowed = frozenset(h.lower() for h in _strs(req.get("allowed_hosts"), 2000))
    since = (now - timedelta(days=pushed_days)).date()
    hits: dict[str, od.RepoHit] = {}
    filtered: list[dict[str, Any]] = []
    try:
        for topic in _strs(req.get("topics"), SEARCH_TOPICS_MAX):
            for item in gh.search_repos(od.build_repo_query(topic, min_stars, since)):
                hit = od.RepoHit.from_item(item, topic)
                if hit is None or hit.full_name.lower() in hits:
                    continue
                reason = od.repo_filter_reason(hit, rules, now, excluded=())
                if reason is not None:
                    filtered.append({"repo": hit.full_name, "reason": reason, "topic": topic})
                    continue
                hits[hit.full_name.lower()] = hit

        def base(h: od.RepoHit) -> float:
            return od.repo_score_base(h, min_stars=min_stars, now=now, languages_preferred=languages,
                                      language_weight=language_weight)  # fmt: skip

        ordered = sorted(hits.values(), key=lambda h: od.rank_key(base(h), h))
        passed: list[tuple[od.RepoHit, od.Probe, str | None]] = []
        for hit in ordered[: 2 * top]:
            try:
                probe = od.probe_repo(gh, hit, now=now, max_age_days=max_branch_days, allowed=allowed, strict=False,
                                      repo_strict=False)  # fmt: skip
                if probe.reason is not None:
                    filtered.append({"repo": hit.full_name, "reason": probe.reason, "topic": hit.topic})
                    continue
                hosts = od.uncovered_hosts(od.egress_hosts(od.precheck_files(gh, hit.full_name)), allowed)
            except GitHubReadError as exc:
                if exc.error_class == UNAVAILABLE:
                    raise
                filtered.append({"repo": hit.full_name, "reason": f"read_failed:{exc.error_class}", "topic": hit.topic})
                continue
            passed.append((hit, probe, od.egress_reason(hosts, repo_strict=False) if hosts else None))
        rows: list[dict[str, Any]] = []
        for i, (hit, probe, strict_skip) in enumerate(passed):
            ci = ci_detect.detect(gh, hit.full_name).status if i < top + 2 else "unknown"
            score = od.repo_score(base(hit), ci_on_pr=ci, ci_weight=ci_weight,
                                  branch_bonus=od.branch_activity_bonus(probe.commit_at, now))  # fmt: skip
            rows.append({"repo": hit.full_name, "stars": hit.stars, "language": hit.language, "ci_on_pr": ci,
                         "pushed_at": hit.pushed_at, "license": hit.license_spdx, "topic": hit.topic,
                         "score": score, "default_branch_commit_at": probe.commit_at,
                         "would_skip_if_strict": strict_skip})  # fmt: skip
    except GitHubRateLimited as exc:
        return {"ok": False, "error": "rate_limited", "until": exc.until, "rows": [], "filtered": []}
    except GitHubReadError as exc:
        return {"ok": False, "error": exc.error_class, "rows": [], "filtered": []}
    by_name = {h.full_name: h for h, _, _ in passed}
    rows.sort(key=lambda r: od.rank_key(float(r["score"]), by_name[str(r["repo"])]))
    return {"ok": True, "rows": rows[:top], "filtered": filtered}


def main() -> int:
    req = json.loads(sys.stdin.read() or "{}")
    branch_days = int(req.get("max_default_branch_age_days") or BRANCH_AGE_DEFAULT)
    if req.get("mode") == "search":
        doc = search(GitHubRead(), req, datetime.now(UTC))
        json.dump(doc, sys.stdout)
        sys.stdout.write("\n")
        return 0 if doc["ok"] else 1
    if req.get("mode") == "facts":
        repos = [str(r) for r in req.get("repos", [])]
        if len(repos) > FACTS_MAX:
            json.dump({"rows": [], "ok": False, "error": f"at most {FACTS_MAX} repos"}, sys.stdout)
            sys.stdout.write("\n")
            return 0
        rows = facts(GitHubRead(), repos, max_branch_days=branch_days)
        json.dump({"rows": rows, "ok": all(r["status"] != "FAIL" for r in rows)}, sys.stdout)
        sys.stdout.write("\n")
        return 0
    repos = [str(r) for r in req.get("repos", [])][:500]
    rows = verify(GitHubRead(), repos, int(req.get("stale_days", 60)), datetime.now(UTC), branch_days)
    json.dump({"rows": rows, "ok": all(r["status"] != "FAIL" for r in rows)}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
