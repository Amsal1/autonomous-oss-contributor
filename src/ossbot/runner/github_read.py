"""Read-only GitHub client of the runner (DESIGN §9.4, §9.5, §9.5a, §9.12; §23 item 33).

``GITHUB_READ_TOKEN`` (fine-grained, public repos read) from ``runner.env``; every request goes
through proxy 3140 (the runner's egress port, ``github_read`` only). The class exposes GET only:
there is no method that could send another verb (asserted by a unit test). Search calls keep a
2.1 s minimum gap (30 requests per minute for authenticated search).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import httpx

API_BASE = "https://api.github.com"
RUNNER_PROXY = "http://10.212.0.2:3140"
API_VERSION = "2022-11-28"
SEARCH_GAP_S = 2.1


class GitHubReadError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def transient(self) -> bool:
        return self.status is None or self.status >= 500 or self.status == 429


def make_client(token: str | None, *, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION, "User-Agent": "ossbot"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if transport is not None:
        return httpx.Client(base_url=API_BASE, headers=headers, transport=transport, timeout=30.0)
    return httpx.Client(base_url=API_BASE, headers=headers, proxy=RUNNER_PROXY, timeout=30.0, trust_env=False)


class GitHubRead:
    """GET-only. ``client`` is injected in tests (``tests/fakes/fake_github.py`` transport)."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or make_client(os.environ.get("GITHUB_READ_TOKEN"))
        self._search_lock = threading.Lock()
        self._last_search = 0.0

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise GitHubReadError(f"GET {path}: {type(exc).__name__}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise GitHubReadError(f"GET {path}: HTTP {resp.status_code}", resp.status_code)
        return resp.json()

    def _list(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        doc = self._get(path, params)
        return [d for d in doc if isinstance(d, dict)] if isinstance(doc, list) else []

    # ------------------------------------------------------------------ repos
    def repo(self, repo: str) -> dict[str, Any] | None:
        doc = self._get(f"/repos/{repo}")
        return doc if isinstance(doc, dict) else None

    def file_text(self, repo: str, path: str) -> str | None:
        """Raw text of one file on the default branch (CONTRIBUTING, AI policy, PR template)."""
        doc = self._get(f"/repos/{repo}/contents/{path}")
        if not isinstance(doc, dict) or doc.get("type") != "file" or doc.get("encoding") != "base64":
            return None
        import base64  # noqa: PLC0415 - only here

        try:
            return base64.b64decode(str(doc.get("content", ""))).decode("utf-8", "replace")[:200_000]
        except ValueError:
            return None

    # ----------------------------------------------------------------- issues
    def issues(self, repo: str, label: str | None = None, per_page: int = 50) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"state": "open", "sort": "updated", "per_page": per_page}
        if label:
            params["labels"] = label
        return self._list(f"/repos/{repo}/issues", params)

    def issue(self, repo: str, number: int) -> dict[str, Any] | None:
        doc = self._get(f"/repos/{repo}/issues/{number}")
        return doc if isinstance(doc, dict) else None

    def comments(self, repo: str, number: int, since: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"per_page": 100}
        if since:
            params["since"] = since
        return self._list(f"/repos/{repo}/issues/{number}/comments", params)

    # ------------------------------------------------------------------ pulls
    def pulls(self, repo: str, state: str = "closed", per_page: int = 30) -> list[dict[str, Any]]:
        return self._list(f"/repos/{repo}/pulls", {"state": state, "sort": "updated", "direction": "desc",
                                                    "per_page": per_page})  # fmt: skip

    def pull(self, repo: str, number: int) -> dict[str, Any] | None:
        doc = self._get(f"/repos/{repo}/pulls/{number}")
        return doc if isinstance(doc, dict) else None

    def pull_files(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self._list(f"/repos/{repo}/pulls/{number}/files", {"per_page": 100})

    def pull_reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self._list(f"/repos/{repo}/pulls/{number}/reviews", {"per_page": 100})

    def check_runs(self, repo: str, sha: str) -> list[dict[str, Any]]:
        doc = self._get(f"/repos/{repo}/commits/{sha}/check-runs", {"per_page": 100})
        runs = doc.get("check_runs") if isinstance(doc, dict) else None
        return [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else []

    # ----------------------------------------------------------------- search
    def open_prs_mentioning(self, repo: str, number: int) -> int:
        """Open PRs that mention the issue (``repo:{r} is:pr is:open {n}``), rate-limited."""
        with self._search_lock:
            wait = SEARCH_GAP_S - (time.monotonic() - self._last_search)
            if wait > 0:
                time.sleep(wait)
            self._last_search = time.monotonic()
            doc = self._get("/search/issues", {"q": f"repo:{repo} is:pr is:open {number}", "per_page": 1})
        return int(doc.get("total_count", 0)) if isinstance(doc, dict) else 0
