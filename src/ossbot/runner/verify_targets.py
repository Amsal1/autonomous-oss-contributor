"""``ossctl targets verify`` worker side (DESIGN §17): read every enabled target with the read token.

Runs as ``ossrunner`` (``ossctl`` starts it with ``systemd-run --uid=ossrunner`` and
``EnvironmentFile=runner.env``), so ``GITHUB_READ_TOKEN`` never enters the operator's process and
the request leaves through proxy 3140 like every runner read. Input on stdin: ``{"repos": [...],
"stale_days": N}``. Output on stdout: one JSON object ``{"rows": [...], "ok": bool}``. GET only;
the token value is never printed.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any

from ossbot.common.clock import from_iso
from ossbot.runner.github_read import GitHubRead, GitHubReadError


def verify(gh: GitHubRead, repos: list[str], stale_days: int, now: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for repo in repos:
        row: dict[str, Any] = {"repo": repo}
        try:
            info = gh.repo(repo)
        except GitHubReadError as exc:
            rows.append({**row, "status": "FAIL", "detail": str(exc)[:120]})
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


def main() -> int:
    req = json.loads(sys.stdin.read() or "{}")
    repos = [str(r) for r in req.get("repos", [])][:500]
    rows = verify(GitHubRead(), repos, int(req.get("stale_days", 60)), datetime.now(UTC))
    json.dump({"rows": rows, "ok": all(r["status"] != "FAIL" for r in rows)}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
