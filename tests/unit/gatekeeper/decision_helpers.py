"""Shared fixtures for the decision-service tests (DESIGN §14.18.4): fake proposal and skill
sources (their real content arrives with PLAN items 29 and 36) and a recording Bot API sender."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.outbox import SendError
from tests.conftest import insert_contribution

CHAT = 4242


@dataclass
class FakeProposals:
    fields_by_key: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)

    def fields(self, conn: sqlite3.Connection, cid: str, version: int) -> dict[str, Any]:
        return dict(self.fields_by_key.get((cid, version), {"proposal_sha256": "a" * 64, "est_class": "s"}))


@dataclass
class FakeSkills:
    fields_by_review: dict[str, dict[str, Any]] = field(default_factory=dict)
    staged: dict[str, str] = field(default_factory=dict)  # skill -> skill_review_id

    def fields(self, conn: sqlite3.Connection, skill_review_id: str) -> dict[str, Any]:
        return dict(self.fields_by_review.get(skill_review_id, {"content_sha256": "c" * 64, "tier": "self_improve"}))

    def reissuable(self, conn: sqlite3.Connection, skill: str) -> str | None:
        return self.staged.get(skill)


class _State:
    def __init__(self, value: str) -> None:
        self.value = value


def install_sources(ctx: AppContext) -> tuple[FakeProposals, FakeSkills]:
    proposals, skills = FakeProposals(), FakeSkills()
    ctx.decisions.proposals.source = proposals
    ctx.decisions.skills.source = skills
    return proposals, skills


def proposed_contribution(ctx: AppContext) -> str:
    return insert_contribution(ctx, _State("PROPOSED"))  # type: ignore[arg-type]  # CR-004 state, PLAN item 29


def proposal_card(ctx: AppContext, cid: str, version: int = 1, message_id: int = 700) -> tuple[str, dict[str, str]]:
    pid = ctx.decisions.proposals.create(cid, version, tg_chat_id=CHAT)
    ctx.decisions.proposals.set_card_message(pid, message_id)
    return pid, ctx.decisions.proposals.issue_card_tokens(pid)


def skill_card(ctx: AppContext, skill: str = "git-helper", parts: int = 2, message_id: int = 800,
               review_id: str = "sr_1") -> tuple[str, dict[str, str]]:  # fmt: skip
    sid = ctx.decisions.skills.create(review_id, skill, 1, tg_chat_id=CHAT, body_parts_total=parts)
    ctx.decisions.skills.set_card_message(sid, message_id)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE skill_approvals SET body_parts_delivered = ? WHERE id = ?", (parts, sid))
    return sid, ctx.decisions.skills.issue_card_tokens(sid)


def status_of(ctx: AppContext, table: str, approval_id: str) -> str:
    row = db.one(ctx.conn, f"SELECT status FROM {table} WHERE id = ?", (approval_id,))
    assert row is not None
    return str(row["status"])


class RecordingSender:
    """Fake Bot API: message ids from 1000 up; optional one-shot failures by dedupe-key-free index."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.next_id = 1000
        self.fail_once: set[int] = set()  # call numbers (0-based) that fail once with a 429
        self.calls = 0

    async def send(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        call = self.calls
        self.calls += 1
        if call in self.fail_once:
            self.fail_once.discard(call)
            raise SendError("429 retry after 0s", retry_after=0.0)
        self.sent.append((method, payload))
        if method.startswith("send"):
            self.next_id += 1
            return {"message_id": self.next_id, "chat": {"id": payload.get("chat_id")}}
        return {"result": True}
