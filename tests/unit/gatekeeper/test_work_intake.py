"""User-directed work (CR-004 Amendment B; DESIGN §9.14, §14.11.19, §14.20.6): POST /v1/work."""

from __future__ import annotations

from typing import Any

import pytest

from ossbot.common.config import KEPT_PRIORITY, OPEN_CATEGORY
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from tests.conftest import UID_OPERATOR, UID_REQUESTER, UID_WORKER, insert_contribution
from tests.unit.gatekeeper.api_helpers import client


@pytest.mark.parametrize(
    ("url", "ref", "issue"),
    [
        ("https://github.com/langfuse/langfuse/issues/1234", "issue", 1234),
        ("github.com/langfuse/langfuse/issues/1234?x=1#c", "issue", 1234),
        ("https://www.github.com/langfuse/langfuse/pull/77", "pull", None),
        ("https://github.com/langfuse/langfuse", "repo", None),
        ("https://github.com/langfuse/langfuse/", "repo", None),
    ],
)
async def test_accepted_urls(ctx: AppContext, url: str, ref: str, issue: int | None) -> None:
    async with client(ctx, UID_OPERATOR) as c:
        resp = await c.post("/v1/work", json={"url": url, "notes": "keep it minimal"})
    assert resp.status_code == 201, resp.text
    doc = resp.json()
    row = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (doc["cid"],))
    assert row is not None
    assert (row["source"], row["state"], row["work_ref"], row["issue_number"]) == ("user", "DISCOVERED", ref, issue)
    assert row["notes"] == "keep it minimal" and row["work_url"].startswith("https://github.com/langfuse/langfuse")
    texts = [r[0] for r in ctx.conn.execute("SELECT payload_json FROM tg_outbox")]
    assert any("👤 requested by you" in t for t in texts)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/langfuse/langfuse/issues/1",
        "https://gitlab.com/langfuse/langfuse/issues/1",
        "https://github.com.evil.example/langfuse/langfuse",
        "https://github.com/langfuse/langfuse/issues/0",
        "https://github.com/langfuse/langfuse/commit/abc",
        "file:///etc/passwd",
        "javascript:alert(1)",
    ],
)
async def test_non_github_urls_refused(ctx: AppContext, url: str) -> None:
    async with client(ctx, UID_OPERATOR) as c:
        resp = await c.post("/v1/work", json={"url": url})
    assert resp.status_code == 400 and resp.json()["error"] == "not_a_github_url"
    assert ctx.conn.execute("SELECT COUNT(*) FROM contributions").fetchone()[0] == 0


OPEN = "openfaas/faas"  # not in config/targets.yaml


class FakeReader:
    """The gatekeeper's read-only GitHub client (GET only) for the /work repo checks."""

    def __init__(self, doc: dict[str, Any] | None, commit_at: str | None = "2026-10-10T00:00:00Z",
                 fail: bool = False) -> None:  # fmt: skip
        self.doc, self.commit_at, self.fail = doc, commit_at, fail
        self.calls: list[str] = []

    async def repo(self, full_name: str) -> dict[str, Any] | None:
        self.calls.append(f"repo {full_name}")
        if self.fail:
            raise RuntimeError("github down")
        return self.doc

    async def default_branch_commit_at(self, full_name: str, branch: str) -> str | None:
        self.calls.append(f"commits {full_name}@{branch}")
        return self.commit_at


def repo_doc(**over: Any) -> dict[str, Any]:
    return {"full_name": OPEN, "private": False, "visibility": "public", "archived": False, "disabled": False,
            "fork": False, "has_issues": True, "has_pull_requests": True, "pull_request_creation_policy": "all",
            "default_branch": "master", "stargazers_count": 25000, "language": "Go",
            "license": {"spdx_id": "MIT"}, "pushed_at": "2026-10-20T00:00:00Z", **over}  # fmt: skip


async def work(ctx: AppContext, url: str) -> Any:
    async with client(ctx, UID_OPERATOR) as c:
        return await c.post("/v1/work", json={"url": url})


async def test_unknown_repo_without_a_github_read_refused(ctx: AppContext) -> None:
    """Owner addition (2026-10-21): any public repo works, but only after the hard checks read GitHub."""
    resp = await work(ctx, "https://github.com/nobody/nothing/issues/3")
    assert resp.status_code == 503 and resp.json()["error"] == "repo_unreadable"
    ctx.github_reader = FakeReader(None, fail=True)
    assert (await work(ctx, "https://github.com/nobody/nothing/issues/3")).json()["error"] == "repo_unreadable"
    assert ctx.conn.execute("SELECT COUNT(*) FROM contributions").fetchone()[0] == 0
    assert ctx.discovered.list() == []


async def test_non_target_repo_is_kept_and_queued(ctx: AppContext) -> None:
    ctx.github_reader = reader = FakeReader(repo_doc())
    resp = await work(ctx, f"https://github.com/{OPEN}/issues/12")
    assert resp.status_code == 201, resp.text
    doc = resp.json()
    assert (doc["repo"], doc["kept_added"], doc["warning"]) == (OPEN, True, None)
    assert reader.calls == [f"repo {OPEN}", f"commits {OPEN}@master"]
    row = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (doc["cid"],))
    assert row is not None and (row["source"], row["category"], row["priority"]) == ("user", OPEN_CATEGORY,
                                                                                     KEPT_PRIORITY)  # fmt: skip
    kept = ctx.discovered.get(OPEN)
    assert kept is not None and (kept.status, kept.source, kept.decided_by) == ("kept", "owner", "uid:1000/operator")
    assert (kept.stars, kept.language, kept.license_spdx) == (25000, "Go", "MIT")
    assert kept.default_branch_commit_at == "2026-10-10T00:00:00Z"
    assert ctx.discovered.effective_target(OPEN) is not None  # runner target() and /package resolve it
    texts = [r[0] for r in ctx.conn.execute("SELECT payload_json FROM tg_outbox")]
    assert any(f"Added {OPEN} to your kept targets" in t for t in texts)
    assert any(f"⭐ Kept {OPEN}" in t for t in texts)
    again = await work(ctx, f"https://github.com/{OPEN}/issues/13")  # already kept: no second keep
    assert again.status_code == 201 and again.json()["kept_added"] is False


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"private": True, "visibility": "private"}, "private"),
        ({"visibility": "internal"}, "private"),
        ({"archived": True}, "archived"),
        ({"disabled": True}, "archived"),
        ({"fork": True}, "fork"),
        ({"has_issues": False}, "issues_disabled"),
        ({"has_pull_requests": False}, "prs_disabled"),
        ({"pull_request_creation_policy": "collaborators_only"}, "pr_creation_restricted"),
        ({"full_name": "other/repo"}, "not_found"),
    ],
)
async def test_hard_checks_refuse(ctx: AppContext, over: dict[str, Any], code: str) -> None:
    ctx.github_reader = FakeReader(repo_doc(**over))
    resp = await work(ctx, f"https://github.com/{OPEN}")
    assert (resp.status_code, resp.json()["error"]) == (400, code)
    assert ctx.discovered.get(OPEN) is None
    assert ctx.conn.execute("SELECT COUNT(*) FROM contributions").fetchone()[0] == 0


async def test_missing_repo_and_never_blocked_refused(ctx: AppContext) -> None:
    ctx.github_reader = FakeReader(None)
    assert (await work(ctx, f"https://github.com/{OPEN}")).json()["error"] == "not_found"
    ctx.github_reader = reader = FakeReader(repo_doc())
    ctx.discovered.decide(OPEN, "never", "uid:1000/operator")
    resp = await work(ctx, f"https://github.com/{OPEN}/issues/1")
    assert (resp.status_code, resp.json()["error"]) == (400, "never_repo")
    assert reader.calls == []  # refused before any read
    assert ctx.discovered.get(OPEN).status == "never"  # type: ignore[union-attr]


async def test_inactive_default_branch_warns_not_refuses(ctx: AppContext) -> None:
    ctx.github_reader = FakeReader(repo_doc(), commit_at="2026-02-01T00:00:00Z")  # ~8 months before the clock
    resp = await work(ctx, f"https://github.com/{OPEN}/issues/4")
    assert resp.status_code == 201
    assert resp.json()["warning"].startswith("⚠️ last commit on master: ") and "months ago" in resp.json()["warning"]
    ctx.github_reader = FakeReader(repo_doc(full_name="o/unknown-age"), commit_at=None)
    resp = await work(ctx, "https://github.com/o/unknown-age")
    assert resp.status_code == 201 and resp.json()["warning"] is None  # unknown is not a warning or a refusal


async def test_config_target_behaves_as_before(ctx: AppContext) -> None:
    ctx.github_reader = reader = FakeReader(repo_doc())
    resp = await work(ctx, "https://github.com/langfuse/langfuse/issues/5")
    assert resp.status_code == 201 and resp.json()["kept_added"] is False
    assert reader.calls == [] and ctx.discovered.list() == []


async def test_roles(ctx: AppContext) -> None:
    for uid in (UID_WORKER, UID_REQUESTER):
        async with client(ctx, uid) as c:
            resp = await c.post("/v1/work", json={"url": "https://github.com/langfuse/langfuse/issues/9"})
        assert resp.status_code == 403


async def test_history_is_skipped_but_active_duplicate_refused(ctx: AppContext) -> None:
    insert_contribution(ctx, State.REJECTED, issue_number=55)  # autonomous discovery would never retry this
    async with client(ctx, UID_OPERATOR) as c:
        ok = await c.post("/v1/work", json={"url": "https://github.com/langfuse/langfuse/issues/55"})
        assert ok.status_code == 201
        dup = await c.post("/v1/work", json={"url": "https://github.com/langfuse/langfuse/issues/55"})
        assert dup.status_code == 409 and dup.json()["error"] == "duplicate_active"


async def test_notes_bounded(ctx: AppContext) -> None:
    async with client(ctx, UID_OPERATOR) as c:
        resp = await c.post("/v1/work", json={"url": "https://github.com/langfuse/langfuse", "notes": "x" * 1001})
    assert resp.status_code == 400


async def test_discover_request_is_held_for_the_runner(ctx: AppContext) -> None:
    """``ossctl discover``: operator request, consumed once by the worker; only discovery targets."""
    async with client(ctx, UID_OPERATOR) as c:
        resp = await c.post("/v1/discover", json={"repos": ["https://github.com/langfuse/langfuse"]})
        assert resp.status_code == 202 and resp.json()["repos"] == ["langfuse/langfuse"]
        assert (await c.post("/v1/discover", json={"repos": ["nobody/nothing"]})).json()["error"] == "not_a_target"
        assert (await c.post("/v1/discover", json={"repos": ["tobenwarrior/hermes-kiro-provider"]})).status_code == 400
    async with client(ctx, UID_WORKER) as c:
        assert (await c.post("/v1/discover/claim")).json() == {"requested": True, "repos": ["langfuse/langfuse"]}
        assert (await c.post("/v1/discover/claim")).json() == {"requested": False, "repos": []}
