"""ForceReply correlation (DESIGN §14.10.4 rule 3, §14.20.2): 15 min TTL, matched only by
``reply_to_message.message_id`` in the owner chat, single consumption."""

from __future__ import annotations

from datetime import timedelta

import pytest

from ossbot.common.clock import FakeClock
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import inputs, size_pickers
from ossbot.gatekeeper.telegram.pending_input import INPUT_KINDS, PendingInputs

OWNER = 4242


def test_matched_only_by_reply_target(ctx: AppContext) -> None:
    pending = PendingInputs(ctx.conn, ctx.clock)
    pending.open("changes", chat_id=OWNER, prompt_message_id=70, cid="c_0000000001", rev=1)
    assert pending.peek(OWNER, 71) is None  # another message
    assert pending.peek(OWNER + 1, 70) is None  # another chat
    assert pending.peek(OWNER, None) is None  # not a reply
    prompt = pending.consume(OWNER, 70)
    assert prompt is not None and prompt.kind == "changes" and prompt.cid == "c_0000000001" and prompt.rev == 1


def test_single_consumption(ctx: AppContext) -> None:
    pending = PendingInputs(ctx.conn, ctx.clock)
    pending.open("edit_title", chat_id=OWNER, prompt_message_id=80)
    assert pending.consume(OWNER, 80) is not None
    assert pending.consume(OWNER, 80) is None  # a replayed reply finds nothing


def test_expires_after_15_minutes(ctx: AppContext, clock: FakeClock) -> None:
    pending = PendingInputs(ctx.conn, ctx.clock)
    pending.open("comment", chat_id=OWNER, prompt_message_id=90, path="a.py", side="new", line_candidates=[1, 2])
    clock.advance(timedelta(minutes=14, seconds=59))
    prompt = pending.peek(OWNER, 90)
    assert prompt is not None and prompt.line_candidates == (1, 2)
    clock.advance(timedelta(seconds=2))
    assert pending.consume(OWNER, 90) is None
    clock.advance(timedelta(days=2))
    assert pending.gc() == 1


def test_open_topic_add_prompt_needs_no_contribution(ctx: AppContext) -> None:
    """Post-v1 open discovery (design §3.2): /filters "Add topic…" opens a prompt without a cid."""
    pending = PendingInputs(ctx.conn, ctx.clock)
    pending.open("open_topic_add", chat_id=OWNER, prompt_message_id=95)
    prompt = pending.consume(OWNER, 95)
    assert prompt is not None and prompt.kind == "open_topic_add" and prompt.cid is None
    assert inputs.INPUTS["open_topic_add"] is size_pickers.on_open_topic_add


def test_kind_set_matches_the_migration(ctx: AppContext) -> None:
    sql = ctx.conn.execute("SELECT sql FROM sqlite_master WHERE name = 'pending_input'").fetchone()[0]
    for kind in INPUT_KINDS:
        assert f"'{kind}'" in sql
    pending = PendingInputs(ctx.conn, ctx.clock)
    with pytest.raises(ValueError):
        pending.open("approve", chat_id=OWNER, prompt_message_id=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        pending.open("changes", chat_id=OWNER, prompt_message_id=1, approve=True)
