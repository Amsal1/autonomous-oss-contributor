"""The runner's GitHub rate limits, for ``ossctl doctor`` (discovery fix 6, ADR-0003 addendum 2026-10-22).

Run as ``ossrunner`` (``runuser``) with ``GITHUB_READ_TOKEN`` in the environment and proxy 3140, like the
runner itself: one ``GET /rate_limit`` (it does not count against the limit) and one JSON line on stdout,
``{"core": {"limit", "used", "remaining", "reset"}, "search": {...}}`` with integers only (anything else is
``null``). A failed read prints ``{"error": "<class>"}`` and exits 1. Neither the token nor a header is
ever printed. Imports only the runner's read client and the standard library.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from ossbot.runner.github_read import GitHubRateLimited, GitHubRead, GitHubReadError

BUCKETS = ("core", "search")
FIELDS = ("limit", "used", "remaining", "reset")


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def summary(doc: dict[str, Any]) -> dict[str, dict[str, int | None]]:
    """``resources.core`` and ``resources.search`` of a ``GET /rate_limit`` answer, integers only."""
    resources = doc.get("resources")
    if not isinstance(resources, dict):
        resources = {}
    out: dict[str, dict[str, int | None]] = {}
    for bucket in BUCKETS:
        raw = resources.get(bucket)
        if not isinstance(raw, dict):
            raw = {}
        out[bucket] = {name: _int(raw.get(name)) for name in FIELDS}
    return out


def main(read: GitHubRead | None = None) -> int:
    try:
        doc = (read or GitHubRead()).rate_limit()
    except GitHubRateLimited:
        print(json.dumps({"error": "github_rate_limited"}))
        return 1
    except GitHubReadError as exc:
        print(json.dumps({"error": exc.error_class}))
        return 1
    if doc is None:
        print(json.dumps({"error": "github_http_404"}))
        return 1
    print(json.dumps(summary(doc), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
