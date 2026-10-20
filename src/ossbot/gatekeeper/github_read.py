"""GitHub REST reads for the gatekeeper (identity check, live checks, restart reconciliation).

Only GET requests live here; every write is in ``github_writer`` (importable only by ``submit``).
The client goes through proxy 3150 (the gatekeeper's egress port) and carries the write token,
which is never logged (the redacting log formatter also knows it).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

API_BASE = "https://api.github.com"
GATEKEEPER_PROXY = "http://10.212.0.2:3150"
API_VERSION = "2022-11-28"


class GitHubError(Exception):
    """A GitHub call failed. ``transient``: network or 5xx (retry); ``auth``: 401/403."""

    def __init__(self, message: str, *, status: int | None = None, transient: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.transient = transient

    @property
    def auth(self) -> bool:
        return self.status in {401, 403}


def make_client(
    token: str | None, *, transport: httpx.AsyncBaseTransport | None = None, proxy: str | None = GATEKEEPER_PROXY
) -> httpx.AsyncClient:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION, "User-Agent": "ossbot"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if transport is not None:
        return httpx.AsyncClient(base_url=API_BASE, headers=headers, transport=transport, timeout=30.0)
    return httpx.AsyncClient(base_url=API_BASE, headers=headers, proxy=proxy, timeout=30.0, trust_env=False)


def raise_for(resp: httpx.Response, what: str) -> None:
    if resp.status_code >= 500:
        raise GitHubError(f"{what}: HTTP {resp.status_code}", status=resp.status_code, transient=True)
    if resp.status_code >= 400:
        raise GitHubError(f"{what}: HTTP {resp.status_code}", status=resp.status_code)


@dataclass(frozen=True)
class TokenIdentity:
    login: str
    scopes: frozenset[str]


class GitHubReader:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def get(self, path: str, params: dict[str, str] | None = None) -> httpx.Response:
        try:
            return await self.client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise GitHubError(f"GET {path}: {type(exc).__name__}", transient=True) from exc

    async def json_or_none(self, path: str, params: dict[str, str] | None = None) -> Any:
        resp = await self.get(path, params)
        if resp.status_code == 404:
            return None
        raise_for(resp, f"GET {path}")
        return resp.json()

    async def identity(self) -> TokenIdentity:
        resp = await self.get("/user")
        raise_for(resp, "GET /user")
        login = resp.json().get("login")
        if not isinstance(login, str):
            raise GitHubError("GET /user without login")
        scopes = frozenset(s.strip() for s in resp.headers.get("X-OAuth-Scopes", "").split(",") if s.strip())
        return TokenIdentity(login, scopes)

    async def repo(self, full_name: str) -> dict[str, Any] | None:
        doc = await self.json_or_none(f"/repos/{full_name}")
        return doc if isinstance(doc, dict) else None

    async def issue(self, upstream: str, number: int) -> dict[str, Any] | None:
        doc = await self.json_or_none(f"/repos/{upstream}/issues/{number}")
        return doc if isinstance(doc, dict) else None

    async def pull(self, upstream: str, number: int) -> dict[str, Any] | None:
        doc = await self.json_or_none(f"/repos/{upstream}/pulls/{number}")
        return doc if isinstance(doc, dict) else None

    async def pulls(self, upstream: str, *, state: str = "open", head: str | None = None) -> list[dict[str, Any]]:
        params = {"state": state, "per_page": "100"}
        if head is not None:
            params["head"] = head
        doc = await self.json_or_none(f"/repos/{upstream}/pulls", params)
        return [d for d in doc if isinstance(d, dict)] if isinstance(doc, list) else []

    async def branch_tip(self, full_name: str, branch: str) -> str | None:
        doc = await self.json_or_none(f"/repos/{full_name}/git/ref/heads/{branch}")
        if not isinstance(doc, dict):
            return None
        sha = (doc.get("object") or {}).get("sha")
        return sha if isinstance(sha, str) else None

    async def comments_since(self, upstream: str, number: int, since: str) -> list[dict[str, Any]]:
        doc = await self.json_or_none(
            f"/repos/{upstream}/issues/{number}/comments", {"since": since, "per_page": "100"}
        )
        return [d for d in doc if isinstance(d, dict)] if isinstance(doc, list) else []

    async def _list(self, path: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        doc = await self.json_or_none(path, {"per_page": "100", **(params or {})})
        return [d for d in doc if isinstance(d, dict)] if isinstance(doc, list) else []

    # ------------------------------------------------- digest stats and the status card (§14.13.4, §14.11.11)
    async def followers(self, login: str) -> int | None:
        """``GET /users/{login}`` ``followers``."""
        doc = await self.json_or_none(f"/users/{login}")
        value = doc.get("followers") if isinstance(doc, dict) else None
        return value if isinstance(value, int) else None

    async def stars(self, login: str, *, max_pages: int = 10) -> int:
        """Sum of ``stargazers_count`` over the user's non-fork public repos (``type=owner``, paged)."""
        total = 0
        for page in range(1, max_pages + 1):
            repos = await self._list(f"/users/{login}/repos", {"type": "owner", "page": str(page)})
            total += sum(int(r.get("stargazers_count") or 0) for r in repos if not r.get("fork"))
            if len(repos) < 100:
                break
        return total

    async def reviews(self, upstream: str, number: int) -> list[dict[str, Any]]:
        return await self._list(f"/repos/{upstream}/pulls/{number}/reviews")

    async def check_runs(self, upstream: str, sha: str) -> list[dict[str, Any]]:
        doc = await self.json_or_none(f"/repos/{upstream}/commits/{sha}/check-runs", {"per_page": "100"})
        runs = doc.get("check_runs") if isinstance(doc, dict) else None
        return [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else []
