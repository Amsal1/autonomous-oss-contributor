"""User-directed work (CR-004 Amendment B; DESIGN §9.14, §14.11.19, §14.20.6): POST /v1/work."""

from __future__ import annotations

import pytest

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


async def test_unknown_repo_refused(ctx: AppContext) -> None:
    async with client(ctx, UID_OPERATOR) as c:
        resp = await c.post("/v1/work", json={"url": "https://github.com/nobody/nothing/issues/3"})
    assert resp.status_code == 400 and resp.json()["error"] == "not_a_target"


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
