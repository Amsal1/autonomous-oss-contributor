"""Read-only GitHub client of the runner (DESIGN §9.4, §9.5, §9.5a, §9.12; §23 item 33).

``GITHUB_READ_TOKEN`` (fine-grained, public repos read) from ``runner.env``; every request goes
through proxy 3140 (the runner's egress port, ``github_read`` only). The class exposes GET only:
there is no method that could send another verb (asserted by a unit test). Search calls keep a
2.1 s minimum gap (30 requests per minute for authenticated search).

Failures (DESIGN §20): a network error or a 5xx is retried after 2, 8 and 30 s, then raises
``GitHubReadError`` with ``error_class=github_unavailable``; another 4xx (not 404, which reads as
``None``) raises at once as ``github_http_<status>``. ``GitHubReadError`` is a ``StageFailed``, so
a stage needs no handling of its own. A rate limit (403 with ``x-ratelimit-remaining: 0`` or a
``retry-after``, or a 429) raises ``GitHubRateLimited`` (a ``Deferred``: the stage is delayed, not
failed), and every read raises it without a request until the reset, at most 1 h ahead.

Two buckets (post-v1 open discovery, design §7.4): ``limited_until`` (core) and
``search_limited_until``. A limit answer on a ``/search/*`` path, or one with
``x-ratelimit-resource: search``, sets only the search bucket, and a search limit never blocks a core
read. A secondary limit without a reset or ``retry-after`` header (403/429 whose message says
"secondary rate limit") waits ``60 * 2**k`` s, ``k`` = consecutive secondary answers on that bucket
(reset by the next success), capped at 15 min. Searches never send ``advanced_search`` or
``search_type`` (the default lexical syntax, under which a comma label list is an OR).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any, Literal

import httpx

from ossbot.runner.errors import Deferred, StageFailed

log = logging.getLogger(__name__)
API_BASE = "https://api.github.com"
RUNNER_PROXY = "http://10.212.0.2:3140"
API_VERSION = "2022-11-28"
SEARCH_GAP_S = 2.1
RETRY_DELAYS_S = (2.0, 8.0, 30.0)  # §20: timeout / 5xx, then github_unavailable
RATE_LIMIT_MAX_S = 3600.0  # §20: sleep until reset, at most 1 h
RATE_LIMIT_DEFAULT_S = 60.0  # a limit answer without a usable reset or retry-after header
SECONDARY_BASE_S = 60.0  # §7.4: a secondary limit without headers waits 60 * 2**k s ...
SECONDARY_MAX_S = 900.0  # ... capped at 15 min
SECONDARY_MESSAGE = "secondary rate limit"
UNAVAILABLE = "github_unavailable"
Bucket = Literal["core", "search"]


class GitHubReadError(StageFailed):
    """A read that did not succeed: the stage fails with ``error_class`` (``github_unavailable``
    after the retries of a network error or 5xx, ``github_http_<status>`` for another 4xx)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        unavailable = status is None or status >= 500
        super().__init__(UNAVAILABLE if unavailable else f"github_http_{status}", message)
        self.status = status


class GitHubRateLimited(Deferred):
    """GitHub's rate limit: reads wait until ``until`` (epoch seconds); the stage is delayed."""

    def __init__(self, until: float, message: str) -> None:
        super().__init__(message)
        self.until = until


def make_client(token: str | None, *, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION, "User-Agent": "ossbot"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if transport is not None:
        return httpx.Client(base_url=API_BASE, headers=headers, transport=transport, timeout=30.0)
    return httpx.Client(base_url=API_BASE, headers=headers, proxy=RUNNER_PROXY, timeout=30.0, trust_env=False)


class GitHubRead:
    """GET-only. ``client`` is injected in tests (``tests/fakes/fake_github.py`` transport)."""

    def __init__(
        self, client: httpx.Client | None = None, *, sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
    ) -> None:  # fmt: skip
        self._client = client or make_client(os.environ.get("GITHUB_READ_TOKEN"))
        self._search_lock = threading.Lock()
        self._last_search: float | None = None
        self._sleep = sleep
        self._wall = wall
        self._monotonic = monotonic
        self.limited_until = 0.0  # epoch seconds; core reads before it raise GitHubRateLimited
        self.search_limited_until = 0.0  # the same for search requests (§7.4: a separate bucket)
        self._secondary: dict[Bucket, int] = {"core": 0, "search": 0}  # consecutive secondary-limit answers

    @staticmethod
    def _secondary_message(resp: httpx.Response) -> bool:
        try:
            doc = resp.json()
        except ValueError:
            return False
        message = doc.get("message") if isinstance(doc, dict) else None
        return isinstance(message, str) and SECONDARY_MESSAGE in message.lower()

    def _limit(self, resp: httpx.Response, bucket: Bucket) -> float | None:
        """The reset time of a rate-limit answer, or None when the answer is not one."""
        headers = resp.headers
        if resp.status_code not in {403, 429}:
            return None
        headed = headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers
        secondary = not headed and self._secondary_message(resp)
        if resp.status_code == 403 and not headed and not secondary:
            return None  # a plain 403 (no access), not a limit
        now = self._wall()
        try:
            if "retry-after" in headers:
                wait = float(headers["retry-after"])
            elif "x-ratelimit-reset" in headers:
                wait = float(headers["x-ratelimit-reset"]) - now
            elif secondary:  # §7.4: 60, 120, 240 ... s, capped at 15 min, reset by the next success
                wait = min(SECONDARY_BASE_S * 2 ** self._secondary[bucket], SECONDARY_MAX_S)
                self._secondary[bucket] += 1
            else:
                wait = RATE_LIMIT_DEFAULT_S
        except ValueError:
            wait = RATE_LIMIT_DEFAULT_S
        return now + min(max(wait, 1.0), RATE_LIMIT_MAX_S)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        search = path.startswith("/search/")
        for attempt in range(len(RETRY_DELAYS_S) + 1):
            blocked = self.search_limited_until if search else self.limited_until
            if self._wall() < blocked:
                raise GitHubRateLimited(blocked, f"GET {path}: rate limited")
            try:
                resp = self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                error = GitHubReadError(f"GET {path}: {type(exc).__name__}")
                error.__cause__ = exc
            else:
                if resp.status_code == 404:
                    return None
                bucket: Bucket = "search" if search or resp.headers.get("x-ratelimit-resource") == "search" else "core"
                if resp.status_code < 400:
                    self._secondary[bucket] = 0
                    return resp.json()
                until = self._limit(resp, bucket)
                if until is not None:
                    if bucket == "search":
                        self.search_limited_until = until = max(self.search_limited_until, until)
                    else:
                        self.limited_until = until = max(self.limited_until, until)
                    log.info("GitHub rate limit (%s) on GET %s; reads wait %d s", bucket, path,
                             int(until - self._wall()),
                             extra={"event": "github_rate_limited", "bucket": bucket})  # fmt: skip
                    raise GitHubRateLimited(until, f"GET {path}: HTTP {resp.status_code} rate limited")
                error = GitHubReadError(f"GET {path}: HTTP {resp.status_code}", resp.status_code)
                if resp.status_code < 500:
                    raise error
            if attempt == len(RETRY_DELAYS_S):
                log.error("GitHub read failed after %d retries: %s", attempt, error,
                          extra={"event": "github_unavailable"})  # fmt: skip
                raise error
            log.warning("GitHub read failed (%s); retry %d in %.0f s", error, attempt + 1, RETRY_DELAYS_S[attempt],
                        extra={"event": "github_read_retry"})  # fmt: skip
            self._sleep(RETRY_DELAYS_S[attempt])
        raise AssertionError("unreachable")  # pragma: no cover

    def _list(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        doc = self._get(path, params)
        return [d for d in doc if isinstance(d, dict)] if isinstance(doc, list) else []

    # ------------------------------------------------------------------ repos
    def repo(self, repo: str) -> dict[str, Any] | None:
        doc = self._get(f"/repos/{repo}")
        return doc if isinstance(doc, dict) else None

    @staticmethod
    def _decode_file(doc: Any) -> str | None:
        """A contents-API file document -> text (None for a directory, a > 1 MB file or bad base64)."""
        if not isinstance(doc, dict) or doc.get("type") != "file" or doc.get("encoding") != "base64":
            return None
        import base64  # noqa: PLC0415 - only here

        try:
            return base64.b64decode(str(doc.get("content", ""))).decode("utf-8", "replace")[:200_000]
        except ValueError:
            return None

    def file_text(self, repo: str, path: str) -> str | None:
        """Raw text of one file on the default branch (CONTRIBUTING, AI policy, PR template)."""
        return self._decode_file(self._get(f"/repos/{repo}/contents/{path}"))

    def readme(self, repo: str) -> str | None:
        """The repo's README (whatever its name or folder), as GitHub picks it."""
        return self._decode_file(self._get(f"/repos/{repo}/readme"))

    def dir_entries(self, repo: str, path: str) -> list[dict[str, Any]]:
        """The entries (``type``, ``name``, ``path``) of a directory; [] for a file or a 404."""
        return self._list(f"/repos/{repo}/contents/{path}")

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

    def commit_status(self, repo: str, sha: str) -> list[dict[str, Any]]:
        """The commit statuses (``context``, ``state``, ``creator``) of the combined status of a commit."""
        doc = self._get(f"/repos/{repo}/commits/{sha}/status", {"per_page": 100})
        statuses = doc.get("statuses") if isinstance(doc, dict) else None
        return [s for s in statuses if isinstance(s, dict)] if isinstance(statuses, list) else []

    def community_profile(self, repo: str) -> dict[str, Any] | None:
        """``GET /repos/{r}/community/profile`` (rule 13: ``files.contributing``); 404 -> None."""
        doc = self._get(f"/repos/{repo}/community/profile")
        return doc if isinstance(doc, dict) else None

    def default_branch_commit_at(self, repo: str, branch: str) -> str | None:
        """Committer date of the latest commit on ``branch`` (owner addition 2026-10-21: the
        default-branch activity gate), or None when GitHub shows none."""
        doc = self._get(f"/repos/{repo}/commits", {"sha": branch, "per_page": 1})
        first = doc[0] if isinstance(doc, list) and doc and isinstance(doc[0], dict) else {}
        date = ((first.get("commit") or {}).get("committer") or {}).get("date")
        return date if isinstance(date, str) and len(date) <= 40 else None

    # ----------------------------------------------------------------- search
    def _search(self, path: str, params: dict[str, Any]) -> Any:
        """Every search request: one lock, ``SEARCH_GAP_S`` apart (≤ 30 per minute)."""
        with self._search_lock:
            if self._last_search is not None:
                wait = SEARCH_GAP_S - (self._monotonic() - self._last_search)
                if wait > 0:
                    self._sleep(wait)
            self._last_search = self._monotonic()
            return self._get(path, params)

    def _search_items(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        doc = self._search(path, params)
        if not isinstance(doc, dict):
            return []
        if doc.get("incomplete_results") is True:  # the items are used; GitHub timed out part of the search
            log.info("GitHub search incomplete: %s", str(params.get("q"))[:200],
                     extra={"event": "github_search_incomplete"})  # fmt: skip
        items = doc.get("items")
        return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []

    def search_repos(self, q: str) -> list[dict[str, Any]]:
        """``GET /search/repositories`` by stars (open discovery, §5.1); the query is built by
        ``open_discovery.build_repo_query``."""
        return self._search_items("/search/repositories", {"q": q, "sort": "stars", "order": "desc", "per_page": 50})

    def search_issues(self, q: str) -> list[dict[str, Any]]:
        """``GET /search/issues`` by update time (open discovery, §6.1)."""
        return self._search_items("/search/issues", {"q": q, "sort": "updated", "order": "desc", "per_page": 30})

    def open_prs_mentioning(self, repo: str, number: int) -> int:
        """Open PRs that mention the issue (``repo:{r} is:pr is:open {n}``), rate-limited."""
        doc = self._search("/search/issues", {"q": f"repo:{repo} is:pr is:open {number}", "per_page": 1})
        return int(doc.get("total_count", 0)) if isinstance(doc, dict) else 0
