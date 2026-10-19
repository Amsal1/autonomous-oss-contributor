"""``chat_turns`` and ``chat_sessions`` (DESIGN §14.12.6, §14.18.5, §14.20.2; PLAN item 37)."""

from __future__ import annotations

import pytest

from ossbot.gatekeeper.chat.turns import ANSWER_MAX, TurnStore, new_turn_id
from ossbot.gatekeeper.context import AppContext


@pytest.fixture
def turns(ctx: AppContext) -> TurnStore:
    return TurnStore(ctx.conn, ctx.clock)


def test_flat_mode_session_keys_and_new(turns: TurnStore) -> None:
    assert turns.session_key(42) == "tg-42-1"
    assert turns.new_session(42) == "tg-42-2"
    assert turns.new_session(42) == "tg-42-3"
    assert turns.session_key(42) == "tg-42-3" and turns.session_key(7) == "tg-7-1"


def test_turn_ids_are_positive_int63() -> None:
    ids = {new_turn_id() for _ in range(200)}
    assert all(0 < i < 2**63 for i in ids) and len(ids) == 200


def test_turn_lifecycle(ctx: AppContext, turns: TurnStore) -> None:
    tid = turns.start("tg-1-1", 10, origin="media", had_attachments=True)
    assert turns.busy("tg-1-1") and turns.status(tid) == "streaming"
    stored = turns.done(tid, "answer", model="claude-haiku-4.5", tools=["web_search"], answer_message_id=11)
    assert stored == "answer" and turns.status(tid) == "done" and not turns.busy("tg-1-1")
    row = ctx.conn.execute("SELECT * FROM chat_turns WHERE turn_id = ?", (tid,)).fetchone()
    assert row["origin"] == "media" and row["had_attachments"] == 1 and row["answer_message_id"] == 11
    assert row["tools_json"] == '["web_search"]' and row["finished_at"] is not None


def test_finalize_after_done_is_a_no_op(turns: TurnStore) -> None:
    tid = turns.start("tg-1-1", 10)
    turns.done(tid, "answer", model=None, tools=[])
    turns.finalize(tid, cancelled=True, failed=False)
    assert turns.status(tid) == "done"


@pytest.mark.parametrize(("cancelled", "failed", "status"), [(True, False, "stopped"), (False, True, "failed"),
                                                              (True, True, "stopped")])  # fmt: skip
def test_finalize_sets_the_terminal_status_once(turns: TurnStore, cancelled: bool, failed: bool, status: str) -> None:
    tid = turns.start("tg-1-1", 10)
    turns.finalize(tid, cancelled=cancelled, failed=failed)
    turns.finalize(tid, cancelled=False, failed=True)
    assert turns.status(tid) == status


def test_reset_streaming_after_a_restart(turns: TurnStore) -> None:
    a, b = turns.start("tg-1-1", 1), turns.start("tg-2-1", 2)
    turns.done(b, "x", model=None, tools=[])
    assert turns.reset_streaming() == 1
    assert turns.status(a) == "failed" and turns.status(b) == "done"


def test_stored_answer_is_redacted_and_bounded(turns: TurnStore) -> None:
    secret = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"
    tid = turns.start("tg-1-1", 1)
    stored = turns.done(tid, f"leak {secret} " + "a" * (ANSWER_MAX + 50), model=None, tools=["t" * 100] * 100)
    assert secret not in stored and len(stored) <= ANSWER_MAX + 64
