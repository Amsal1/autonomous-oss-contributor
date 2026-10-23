"""``ContributionService.get`` (the runner's view of a contribution): open change requests carry their
source, the parsed ``source_ref`` and the untrusted finding body (owner decision 2026-10-23 (b)); a
redacted read still omits them."""

from __future__ import annotations

import json

import pytest

from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from tests.conftest import insert_contribution

pytestmark = pytest.mark.unit
REF = {"comment_id": 7, "path": "src/x.ts", "line": 120, "author": "coderabbitai[bot]"}


def test_change_requests_carry_source_and_untrusted_text(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.IMPLEMENTING, pr_number=5)
    with db.transaction(ctx.conn):
        ctx.conn.execute("INSERT INTO change_requests (id, cid, text, created_at) VALUES ('r_a', ?, 'rename it',"
                         " '2026-10-23T10:00:00Z')", (cid,))  # fmt: skip
        ctx.conn.execute("INSERT INTO change_requests (id, cid, text, created_at, source, source_ref, untrusted_text)"
                         " VALUES ('r_b', ?, 'fixed', '2026-10-23T10:01:00Z', 'review_bot', ?, 'the finding')",
                         (cid, json.dumps(REF)))  # fmt: skip
        ctx.conn.execute("INSERT INTO change_requests (id, cid, text, created_at, consumed_at) VALUES ('r_c', ?, 'old',"
                         " '2026-10-22T10:00:00Z', '2026-10-22T11:00:00Z')", (cid,))  # fmt: skip
    doc = ctx.contributions.get(cid)
    assert doc is not None
    assert doc["change_requests"] == [
        {"id": "r_a", "text": "rename it", "created_at": "2026-10-23T10:00:00Z", "source": "owner",
         "source_ref": None, "untrusted_text": None},
        {"id": "r_b", "text": "fixed", "created_at": "2026-10-23T10:01:00Z", "source": "review_bot",
         "source_ref": REF, "untrusted_text": "the finding"},
    ]  # fmt: skip
    redacted = ctx.contributions.get(cid, redacted=True)
    assert redacted is not None and "change_requests" not in redacted


def test_a_broken_source_ref_reads_as_none(ctx: AppContext) -> None:
    cid = insert_contribution(ctx, State.IMPLEMENTING, pr_number=5)
    with db.transaction(ctx.conn):
        ctx.conn.execute("INSERT INTO change_requests (id, cid, text, created_at, source, source_ref, untrusted_text)"
                         " VALUES ('r_d', ?, 'fixed', 't', 'review_bot', '[1', 'x')", (cid,))  # fmt: skip
    doc = ctx.contributions.get(cid)
    assert doc is not None and doc["change_requests"][0]["source_ref"] is None
