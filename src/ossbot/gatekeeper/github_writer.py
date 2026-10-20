"""Every GitHub write the system can make: fork, push, pull request, comment (DESIGN §12.5, I1).

Importable only from ``ossbot.gatekeeper.submit`` (import-linter contract). Each call here is
preceded by ``control_checkpoint()`` in ``submit``; nothing in this module decides whether to write.
The push token reaches git through ``GIT_ASKPASS`` reading an inherited memfd: it is never in
argv, the URL, the environment or a log line. A push is never forced (no ``+``, no ``--force``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from ossbot.gatekeeper.github_read import GitHubError, raise_for
from ossbot.gatekeeper.gitops import REF_NAME_RE, SHA_RE, Git, GitError, github_url, push_with_token


class Pusher(Protocol):
    def push(self, mirror: Path, fork_repo: str, head: str, branch: str) -> None: ...


def push_argv(fork_repo: str, login: str, head: str, branch: str) -> list[str]:
    """``git push --no-verify https://<login>@github.com/<fork>.git <head>:refs/heads/<branch>``."""
    if not SHA_RE.match(head) or not REF_NAME_RE.match(branch) or ".." in branch:
        raise ValueError("bad push target")
    url = github_url(fork_repo).replace("https://", f"https://{login}@", 1)
    return ["-c", "credential.helper=", "push", "--no-verify", "--porcelain", url, f"{head}:refs/heads/{branch}"]


class GitPusher:
    def __init__(self, git: Git, token: str, login: str) -> None:
        self.git = git
        self.token = token
        self.login = login

    def push(self, mirror: Path, fork_repo: str, head: str, branch: str) -> None:
        try:
            push_with_token(self.git, self.token, self.login, push_argv(fork_repo, self.login, head, branch),
                            cwd=mirror)  # fmt: skip
        except GitError as exc:
            transient = any(s in exc.stderr for s in ("Could not resolve", "timed out", "Connection", "HTTP 5"))
            raise GitHubError(f"push failed ({exc.returncode})", transient=transient) from exc


@dataclass(frozen=True)
class PullCreated:
    number: int
    url: str


class GitHubWriter:
    def __init__(self, client: httpx.AsyncClient, pusher: Pusher) -> None:
        self.client = client
        self.pusher = pusher

    async def _post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        try:
            return await self.client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise GitHubError(f"POST {path}: {type(exc).__name__}", transient=True) from exc

    async def create_fork(self, upstream: str, name: str) -> dict[str, Any]:
        resp = await self._post(f"/repos/{upstream}/forks", {"name": name, "default_branch_only": True})
        raise_for(resp, "POST forks")
        doc: dict[str, Any] = resp.json()
        return doc

    def push(self, mirror: Path, fork_repo: str, head: str, branch: str) -> None:
        self.pusher.push(mirror, fork_repo, head, branch)

    async def create_pull(self, upstream: str, payload: dict[str, Any]) -> httpx.Response:
        """Returns the raw response: 422 "already exists" is reconciled by the caller."""
        resp = await self._post(f"/repos/{upstream}/pulls", payload)
        if resp.status_code != 422:
            raise_for(resp, "POST pulls")
        return resp

    async def create_comment(self, upstream: str, number: int, body: str) -> dict[str, Any]:
        resp = await self._post(f"/repos/{upstream}/issues/{number}/comments", {"body": body})
        raise_for(resp, "POST comment")
        doc: dict[str, Any] = resp.json()
        return doc
