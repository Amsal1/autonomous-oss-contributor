"""In-memory fake of the GitHub REST API (respx router) plus a fake pusher.

Every request is recorded; ``writes`` lists the non-GET ones, so tests can prove "zero writes"
(dry-run, AC 4) and "reads only" (restart reconciliation). ``on_write`` lets a test flip the kill
or pause flag right after a given write (AC 29).
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import respx

from ossbot.gatekeeper.github_read import GitHubError, make_client

API = "https://api.github.com"


@dataclass
class Recorded:
    method: str
    path: str
    params: dict[str, str]
    body: Any


@dataclass
class FakeGitHub:
    login: str = "test-bot-login"
    scopes: str = "public_repo"
    repos: dict[str, dict[str, Any]] = field(default_factory=dict)
    issues: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    comments: dict[tuple[str, int], list[dict[str, Any]]] = field(default_factory=dict)
    pulls: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    refs: dict[tuple[str, str], str] = field(default_factory=dict)
    files: dict[tuple[str, str], str] = field(default_factory=dict)  # (repo lower, path) -> text
    requests: list[Recorded] = field(default_factory=list)
    failures: dict[str, list[int]] = field(default_factory=dict)  # "POST /repos/x/y/pulls" -> statuses to return first
    fork_visible_after: int = 0  # GETs of a new fork that still 404
    on_write: Callable[[str], None] | None = None
    next_number: int = 500
    # review mirror (FEAT-010 item 51): Actions setting per repo, PR reviews and line comments, the status of
    # a POST git/refs (the enable-time foreign-write probe), and the token-expiration header of GET /user
    actions_enabled: dict[str, bool] = field(default_factory=dict)
    reviews: dict[tuple[str, int], list[dict[str, Any]]] = field(default_factory=dict)
    review_comments: dict[tuple[str, int], list[dict[str, Any]]] = field(default_factory=dict)
    create_ref_status: int = 403
    token_expiration: str | None = None
    router: respx.MockRouter = field(init=False)

    def __post_init__(self) -> None:
        self.router = respx.MockRouter(assert_all_called=False, base_url=API)
        self.router.route().mock(side_effect=self._handle)

    # ------------------------------------------------------------------ setup
    def add_repo(self, full: str, **doc: Any) -> None:
        self.repos[full.lower()] = {"full_name": full, "archived": False, "fork": False, **doc}

    def add_issue(self, repo: str, number: int, **doc: Any) -> None:
        self.issues[(repo.lower(), number)] = {"number": number, "state": "open", "assignees": [], **doc}

    def add_pull(self, repo: str, **doc: Any) -> dict[str, Any]:
        self.next_number += 1
        pr = {"number": self.next_number, "state": "open", "title": "", "body": "", "merged_at": None,
              "html_url": f"https://github.com/{repo}/pull/{self.next_number}", **doc}  # fmt: skip
        self.pulls.setdefault(repo.lower(), []).append(pr)
        return pr

    def client(self, token: str | None = "ghp_fake_write_token") -> httpx.AsyncClient:
        return make_client(token, transport=httpx.MockTransport(self.router.async_handler))

    def sync_client(self, token: str | None = "github_pat_fake_read_token") -> httpx.Client:
        """The runner's read client (``runner.github_read``, sync) on the same fake."""
        from ossbot.runner.github_read import make_client as runner_client

        return runner_client(token, transport=httpx.MockTransport(self.router.handler))

    @property
    def writes(self) -> list[Recorded]:
        return [r for r in self.requests if r.method != "GET"]

    # ---------------------------------------------------------------- handler
    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        body = json.loads(request.content) if request.content else None
        self.requests.append(Recorded(request.method, path, params, body))
        key = f"{request.method} {path}"
        if self.failures.get(key):
            status = self.failures[key].pop(0)
            return httpx.Response(status, json={"message": "injected failure"})
        response = self._dispatch(request.method, path, params, body)
        if request.method != "GET" and response.status_code < 300 and self.on_write is not None:
            self.on_write(key)
        return response

    def _dispatch(self, method: str, path: str, params: dict[str, str], body: Any) -> httpx.Response:
        if path == "/user":
            headers = {"X-OAuth-Scopes": self.scopes}
            if self.token_expiration:
                headers["github-authentication-token-expiration"] = self.token_expiration
            return httpx.Response(200, json={"login": self.login}, headers=headers)
        if path == "/search/issues":
            return httpx.Response(200, json={"total_count": 0, "items": []})
        mu = re.fullmatch(r"/users/([^/]+)/repos", path)
        if mu:
            owner = mu.group(1).lower()
            return httpx.Response(200, json=[d for k, d in sorted(self.repos.items()) if k.split("/")[0] == owner])
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)(/.*)?", path)
        if m is None:
            return httpx.Response(404, json={"message": "Not Found"})
        repo, rest = m.group(1).lower(), m.group(2) or ""
        if rest == "" and method == "GET":
            doc = self.repos.get(repo)
            if doc is not None and doc.get("_hidden", 0) > 0:
                doc["_hidden"] -= 1
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=doc) if doc else httpx.Response(404, json={"message": "Not Found"})
        if rest == "/forks" and method == "POST":
            name = body.get("name") or repo.split("/", 1)[1]
            full = f"{self.login}/{name}"
            self.add_repo(full, fork=True, parent={"full_name": self.repos[repo]["full_name"]},
                          _hidden=self.fork_visible_after)  # fmt: skip
            return httpx.Response(202, json=self.repos[full.lower()])
        m2 = re.fullmatch(r"/issues/(\d+)(/comments)?", rest)
        if m2:
            number = int(m2.group(1))
            if m2.group(2) is None:
                issue = self.issues.get((repo, number))
                return httpx.Response(200, json=issue) if issue else httpx.Response(404, json={})
            if method == "POST":
                comment = {"id": len(self.requests), "body": body["body"], "user": {"login": self.login},
                           "html_url": f"https://github.com/{repo}/issues/{number}#c{len(self.requests)}",
                           "created_at": "2026-10-17T12:00:00Z"}  # fmt: skip
                self.comments.setdefault((repo, number), []).append(comment)
                return httpx.Response(201, json=comment)
            return httpx.Response(200, json=self.comments.get((repo, number), []))
        if rest == "/pulls":
            if method == "POST":
                head = body["head"] if ":" in body["head"] else f"{repo.split('/')[0]}:{body['head']}"  # same-repo PR
                if any(p["head"]["label"] == head for p in self.pulls.get(repo, [])):
                    errors = [{"message": "A pull request already exists"}]
                    return httpx.Response(422, json={"message": "Validation Failed", "errors": errors})
                owner, branch = head.split(":", 1)
                pr = self.add_pull(self.repos[repo]["full_name"], title=body["title"], body=body["body"],
                                   user={"login": owner}, head={"label": head, "ref": branch},
                                   base={"ref": body["base"]})  # fmt: skip
                return httpx.Response(201, json=pr)
            items = self.pulls.get(repo, [])
            if params.get("state", "open") != "all":
                items = [p for p in items if p["state"] == params.get("state", "open")]
            if "head" in params:
                items = [p for p in items if p["head"]["label"] == params["head"]]
            return httpx.Response(200, json=items)
        if rest == "/issues" and method == "GET":  # runner discovery (FEAT-006)
            label = params.get("labels")
            items = [i for (r, _), i in sorted(self.issues.items()) if r == repo and i.get("state") == "open"]
            if label:
                items = [i for i in items if label in {lbl.get("name") for lbl in i.get("labels", [])}]
            return httpx.Response(200, json=items)
        if rest == "/readme" and method == "GET":  # runner vet (README AI wording)
            text = self.files.get((repo, "README.md"))
            if text is None:
                return httpx.Response(404, json={"message": "Not Found"})
            encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
            return httpx.Response(200, json={"type": "file", "encoding": "base64", "content": encoded,
                                             "path": "README.md"})  # fmt: skip
        m5 = re.fullmatch(r"/contents/(.+)", rest)
        if m5 and method == "GET":  # CONTRIBUTING, AI policy, PR template (runner vet); directory listings
            wanted = m5.group(1).rstrip("/")
            text = self.files.get((repo, wanted))
            if text is None:
                prefix = wanted + "/"
                names = sorted({p[len(prefix):].split("/", 1)[0] for (r, p) in self.files
                                if r == repo and p.startswith(prefix)})  # fmt: skip
                if not names:
                    return httpx.Response(404, json={"message": "Not Found"})
                entries = [{"type": "file" if (repo, prefix + n) in self.files else "dir", "name": n,
                            "path": prefix + n} for n in names]  # fmt: skip
                return httpx.Response(200, json=entries)
            encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
            return httpx.Response(200, json={"type": "file", "encoding": "base64", "content": encoded})
        m3 = re.fullmatch(r"/pulls/(\d+)", rest)
        if m3:
            pr = next((p for p in self.pulls.get(repo, []) if p["number"] == int(m3.group(1))), None)
            if pr is not None and method == "PATCH":
                pr.update({k: v for k, v in body.items() if k in {"title", "body", "state"}})
            return httpx.Response(200, json=pr) if pr else httpx.Response(404, json={})
        m6 = re.fullmatch(r"/pulls/(\d+)/(reviews|comments)", rest)
        if m6 and method == "GET":
            store = self.reviews if m6.group(2) == "reviews" else self.review_comments
            return httpx.Response(200, json=store.get((repo, int(m6.group(1))), []))
        if rest == "/actions/permissions" and method == "GET":
            return httpx.Response(200, json={"enabled": self.actions_enabled.get(repo, True)})
        if rest == "/git/refs" and method == "POST":
            return httpx.Response(self.create_ref_status, json={"message": "fake"})
        m7 = re.fullmatch(r"/git/refs/heads/(.+)", rest)
        if m7 and method == "DELETE":
            return httpx.Response(204) if self.refs.pop((repo, m7.group(1)), None) else httpx.Response(422, json={})
        m4 = re.fullmatch(r"/git/ref/heads/(.+)", rest)
        if m4:
            sha = self.refs.get((repo, m4.group(1)))
            return httpx.Response(200, json={"object": {"sha": sha}}) if sha else httpx.Response(404, json={})
        return httpx.Response(404, json={"message": "Not Found"})


@dataclass
class FakePusher:
    """Stands in for ``GitPusher``: records the exact SHA pushed and updates the fake fork ref."""

    gh: FakeGitHub
    pushes: list[tuple[str, str, str]] = field(default_factory=list)
    fail_with: list[GitHubError] = field(default_factory=list)

    def push(self, mirror: Path, fork_repo: str, head: str, branch: str) -> None:
        self.gh.requests.append(Recorded("PUSH", f"{fork_repo}:{branch}", {}, head))
        if self.fail_with:
            raise self.fail_with.pop(0)
        self.pushes.append((fork_repo, head, branch))
        self.gh.refs[(fork_repo.lower(), branch)] = head
        if self.gh.on_write is not None:
            self.gh.on_write("PUSH")
