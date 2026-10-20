"""Every write of the opt-in private review mirror (ADR-0019 option C, DESIGN §14.11.14, U5).

Importable only from ``ossbot.gatekeeper.review_mirror`` (import-linter contract). The writer is bound
to one repo, the configured private mirror, and hard-refuses any API path outside ``/repos/<mirror>/``
and any push URL other than ``https://github.com/<mirror>.git``. It force-pushes the two display
branches ``review/<cid>`` and ``review-base/<cid>`` (force is allowed only here; ``github_writer`` never
forces), opens or updates the mirror PR, closes it and deletes the branches. The token is the separate
fine-grained ``GITHUB_REVIEW_MIRROR_TOKEN``; it reaches git through the askpass memfd, never argv, URL,
environment or a log line. Nothing here decides whether to write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ossbot.gatekeeper.github_read import GitHubError, raise_for
from ossbot.gatekeeper.gitops import SHA_RE, UPSTREAM_RE, Git, GitError, github_url, push_with_token

CID_RE = re.compile(r"^c_[0-9A-HJKMNP-TV-Z]{10}$")


class MirrorWriteRefused(RuntimeError):
    """A write outside the mirror repo, or a malformed ref, was attempted."""


@dataclass(frozen=True)
class MirrorPull:
    number: int
    url: str


def push_argv(repo: str, login: str, head: str, base: str, cid: str) -> list[str]:
    """``git push --force`` of ``H'`` and ``B'`` to the two display branches of ``cid`` (``+`` refspecs)."""
    if not SHA_RE.match(head) or not SHA_RE.match(base) or not CID_RE.match(cid):
        raise MirrorWriteRefused("bad mirror push target")
    url = github_url(repo).replace("https://", f"https://{login}@", 1)
    return ["-c", "credential.helper=", "push", "--no-verify", "--porcelain", "--force", url,
            f"+{head}:refs/heads/review/{cid}", f"+{base}:refs/heads/review-base/{cid}"]  # fmt: skip


class MirrorWriter:
    def __init__(self, client: httpx.AsyncClient, git: Git, token: str, login: str, repo: str) -> None:
        if not UPSTREAM_RE.match(repo):
            raise MirrorWriteRefused("bad mirror repo")
        self.client = client
        self.git = git
        self.token = token
        self.login = login
        self.repo = repo

    def _path(self, path: str) -> str:
        """Every API path must stay under ``/repos/<mirror>/`` (case-insensitive owner/name)."""
        prefix = f"/repos/{self.repo}/"
        if not path.lower().startswith(prefix.lower()) or ".." in path or "//" in path[1:]:
            raise MirrorWriteRefused(f"path outside the mirror: {path[:80]}")
        return path

    async def _send(self, method: str, path: str, payload: dict[str, Any] | None = None) -> httpx.Response:
        try:
            return await self.client.request(method, self._path(path), json=payload)
        except httpx.HTTPError as exc:
            raise GitHubError(f"{method} {path}: {type(exc).__name__}", transient=True) from exc

    def push(self, git_dir: Path, head: str, base: str, cid: str) -> None:
        argv = push_argv(self.repo, self.login, head, base, cid)
        if not argv[6].endswith(f"github.com/{self.repo}.git"):  # pragma: no cover - github_url is the only source
            raise MirrorWriteRefused("push URL outside the mirror")
        try:
            push_with_token(self.git, self.token, self.login, argv, cwd=git_dir)
        except GitError as exc:
            protected = "push declined due to repository rule" in exc.stderr or "GH013" in exc.stderr
            reason = "push protection" if protected else f"push failed ({exc.returncode})"
            raise GitHubError(reason, transient=not protected and "HTTP 5" in exc.stderr) from exc

    async def open_or_update(self, cid: str, title: str, body: str) -> MirrorPull:
        """The mirror PR ``review/<cid>`` → ``review-base/<cid>``: PATCH an open one, else POST."""
        owner = self.repo.split("/", 1)[0]
        resp = await self.client.get(self._path(f"/repos/{self.repo}/pulls"),
                                     params={"state": "open", "head": f"{owner}:review/{cid}"})  # fmt: skip
        raise_for(resp, "GET mirror pulls")
        found = [p for p in resp.json() if isinstance(p, dict)] if isinstance(resp.json(), list) else []
        if found:
            number = int(found[0]["number"])
            resp = await self._send("PATCH", f"/repos/{self.repo}/pulls/{number}", {"title": title, "body": body})
            raise_for(resp, "PATCH mirror pull")
        else:
            resp = await self._send("POST", f"/repos/{self.repo}/pulls", {
                "title": title, "body": body, "head": f"review/{cid}", "base": f"review-base/{cid}",
                "maintainer_can_modify": False})  # fmt: skip
            raise_for(resp, "POST mirror pull")
        doc = resp.json()
        return MirrorPull(int(doc["number"]), str(doc.get("html_url") or ""))

    async def close(self, number: int, cid: str) -> None:
        """Retention: close the mirror PR and delete both branches (404s are fine: already gone)."""
        resp = await self._send("PATCH", f"/repos/{self.repo}/pulls/{int(number)}", {"state": "closed"})
        if resp.status_code != 404:
            raise_for(resp, "close mirror pull")
        await self.delete_branches(cid)

    async def delete_branches(self, cid: str) -> None:
        if not CID_RE.match(cid):
            raise MirrorWriteRefused("bad cid")
        for ref in (f"review/{cid}", f"review-base/{cid}"):
            resp = await self._send("DELETE", f"/repos/{self.repo}/git/refs/heads/{ref}")
            if resp.status_code not in {404, 422}:
                raise_for(resp, "DELETE mirror ref")
