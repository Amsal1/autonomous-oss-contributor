"""Every Telegraph API call the system can make (DESIGN §14.12.8, U11). Opt-in, off by default.

Importable only from ``ossbot.gatekeeper.telegraph`` (import-linter contract). The base URL and the
methods are hard-coded: ``createPage``, ``editPage`` and ``revokeAccessToken``, plus ``createAccount``
for ``ossctl telegraph setup`` (which DESIGN names as the account step). Requests are POSTs with a form
body (never a token in a URL or a log line). The gatekeeper's client goes through proxy 3150, where
``api.telegra.ph`` is rendered only while ``telegraph.enabled`` is true. Nothing here decides whether to
publish: ``telegraph.py`` checks eligibility, the scans and the control state first.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

BASE_URL = "https://api.telegra.ph/"
METHODS = frozenset({"createAccount", "createPage", "editPage", "revokeAccessToken"})
GATEKEEPER_PROXY = "http://10.212.0.2:3150"
PATH_RE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")


class TelegraphError(RuntimeError):
    pass


@dataclass(frozen=True)
class Page:
    path: str
    url: str


def make_client(*, transport: httpx.AsyncBaseTransport | None = None, proxy: str | None = GATEKEEPER_PROXY,
                timeout: float = 20.0) -> httpx.AsyncClient:  # fmt: skip
    if transport is not None:
        return httpx.AsyncClient(base_url=BASE_URL, transport=transport, timeout=timeout)
    return httpx.AsyncClient(base_url=BASE_URL, proxy=proxy, timeout=timeout, trust_env=False)


class TelegraphWriter:
    def __init__(self, client: httpx.AsyncClient, token: str | None) -> None:
        self.client = client
        self.token = token

    async def _call(self, method: str, data: dict[str, str]) -> dict[str, Any]:
        if method not in METHODS:
            raise TelegraphError(f"method {method} is not allowed")
        if str(self.client.base_url) != BASE_URL:
            raise TelegraphError("the Telegraph client must use https://api.telegra.ph/")
        try:
            resp = await self.client.post(method, data=data)
        except httpx.HTTPError as exc:
            raise TelegraphError(f"{method}: {type(exc).__name__}") from exc
        try:
            doc = resp.json()
        except ValueError as exc:
            raise TelegraphError(f"{method}: HTTP {resp.status_code}") from exc
        if resp.status_code != 200 or not isinstance(doc, dict) or doc.get("ok") is not True:
            error = str(doc.get("error") if isinstance(doc, dict) else "")[:80]
            raise TelegraphError(f"{method}: {error or resp.status_code}")
        result = doc.get("result")
        return result if isinstance(result, dict) else {}

    def _need_token(self) -> str:
        if not self.token:
            raise TelegraphError("TELEGRAPH_ACCESS_TOKEN is not set")
        return self.token

    async def create_account(self, short_name: str = "ossbot") -> str:
        result = await self._call("createAccount", {"short_name": short_name})
        token = result.get("access_token")
        if not isinstance(token, str) or not token:
            raise TelegraphError("createAccount: no access_token")
        return token

    async def revoke(self) -> str:
        result = await self._call("revokeAccessToken", {"access_token": self._need_token()})
        token = result.get("access_token")
        if not isinstance(token, str) or not token:
            raise TelegraphError("revokeAccessToken: no access_token")
        return token

    async def create_page(self, title: str, nodes: list[Any], author_name: str = "") -> Page:
        data = {"access_token": self._need_token(), "title": title, "content": json.dumps(nodes, ensure_ascii=False),
                "return_content": "false"}  # fmt: skip
        if author_name:
            data["author_name"] = author_name
        return _page(await self._call("createPage", data))

    async def edit_page(self, path: str, title: str, nodes: list[Any]) -> Page:
        if not path or not set(path) <= PATH_RE_CHARS:
            raise TelegraphError("bad page path")
        return _page(await self._call("editPage", {"access_token": self._need_token(), "path": path, "title": title,
                                                   "content": json.dumps(nodes, ensure_ascii=False)}))  # fmt: skip


def _page(result: dict[str, Any]) -> Page:
    path, url = result.get("path"), result.get("url")
    if not isinstance(path, str) or not isinstance(url, str) or not url.startswith("https://telegra.ph/"):
        raise TelegraphError("unexpected page result")
    return Page(path, url)
