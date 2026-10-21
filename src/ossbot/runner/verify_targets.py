"""``ossctl targets verify`` worker side (DESIGN §17): read every enabled target with the read token.

Runs as ``ossrunner`` (``ossctl`` starts it with ``systemd-run --uid=ossrunner`` and
``EnvironmentFile=runner.env``), so ``GITHUB_READ_TOKEN`` never enters the operator's process and
the request leaves through proxy 3140 like every runner read. Input on stdin: ``{"repos": [...],
"stale_days": N}``, or ``{"mode": "facts", "repos": [...]}`` (≤ 5) for ``ossctl targets facts``.
Output on stdout: one JSON object ``{"rows": [...], "ok": bool}``. GET only; the token value is never
printed.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any

from ossbot.common import ai_policy, ci_facts
from ossbot.common.clock import from_iso
from ossbot.runner import ci_detect
from ossbot.runner.github_read import UNAVAILABLE, GitHubRateLimited, GitHubRead, GitHubReadError
from ossbot.runner.repo_docs import fetch_policy_docs

FACTS_MAX = 5


def verify(gh: GitHubRead, repos: list[str], stale_days: int, now: datetime) -> list[dict[str, Any]]:
    """One row per repo. Once a read fails as ``github_unavailable`` (its retries used up), the
    rest are FAIL without a read, so an outage costs one read's retries, not one per target."""
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
        if info.get("archived"):
            row.update(status="FAIL", detail="archived")
        elif info.get("full_name") and str(info["full_name"]).lower() != repo.lower():
            row.update(status="WARN", detail=f"renamed to {info['full_name']}")
        elif age is not None and age > stale_days:
            row.update(status="WARN", detail=f"no push for {age} days")
        else:
            row.update(status="PASS", detail="")
        rows.append(row)
    return rows


def facts(gh: GitHubRead, repos: list[str]) -> list[dict[str, Any]]:
    """``ossctl targets facts``: the deterministic AI-policy classification and the CI-on-PR facts per
    repo (≤ ``FACTS_MAX``), the same code vet and discovery use. No LLM, no gatekeeper call, no row."""
    rows: list[dict[str, Any]] = []
    for repo in repos[:FACTS_MAX]:
        row: dict[str, Any] = {"repo": repo}
        try:
            if gh.repo(repo) is None:
                rows.append({**row, "status": "FAIL", "detail": "not found (or not readable with the read token)"})
                continue
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


def main() -> int:
    req = json.loads(sys.stdin.read() or "{}")
    if req.get("mode") == "facts":
        repos = [str(r) for r in req.get("repos", [])]
        if len(repos) > FACTS_MAX:
            json.dump({"rows": [], "ok": False, "error": f"at most {FACTS_MAX} repos"}, sys.stdout)
            sys.stdout.write("\n")
            return 0
        rows = facts(GitHubRead(), repos)
        json.dump({"rows": rows, "ok": all(r["status"] != "FAIL" for r in rows)}, sys.stdout)
        sys.stdout.write("\n")
        return 0
    repos = [str(r) for r in req.get("repos", [])][:500]
    rows = verify(GitHubRead(), repos, int(req.get("stale_days", 60)), datetime.now(UTC))
    json.dump({"rows": rows, "ok": all(r["status"] != "FAIL" for r in rows)}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
