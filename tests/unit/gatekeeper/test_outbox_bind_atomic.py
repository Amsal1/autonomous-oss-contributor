"""Delta 6 (DESIGN §14.18.2 step 3): Outbox.mark_sent binds kit tokens, writes the tg_views row,
bumps the thread counter and counts card parts (by id prefix) in one transaction."""

from __future__ import annotations

from datetime import timedelta

import pytest

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.outbox import OutboxWorker
from tests.unit.gatekeeper.decision_helpers import CHAT, RecordingSender, install_sources


class Crash(Exception):
    pass


def _kit_message(ctx: AppContext) -> tuple[str, int]:
    token = ctx.approvals.mint_token("nav", chat_id=CHAT, message_id=None,
                                     expires_at=ctx.clock.now() + timedelta(hours=24), cid="c_X")  # fmt: skip
    row_id = ctx.outbox.enqueue(
        "sendMessage", {"chat_id": CHAT, "text": "explorer"}, dedupe_key="kit:1", bind_tokens=[token],
        view={"kind": "explorer", "cid": "c_X", "rev": 1, "action_id": "x_1", "state": {"section": 1}},
    )  # fmt: skip
    assert row_id is not None
    return token, row_id


async def test_crash_after_mark_sent_keeps_binding_and_view(ctx: AppContext) -> None:
    token, _ = _kit_message(ctx)

    async def crash(row: object, result: object) -> None:
        raise Crash  # the process dies after mark_sent, before any on_sent work

    sender = RecordingSender()
    await OutboxWorker(ctx.outbox, sender, crash).run_once()
    mid = sender.next_id
    tok = db.one(ctx.conn, "SELECT tg_message_id FROM ui_tokens WHERE token = ?", (token,))
    view = db.one(ctx.conn, "SELECT * FROM tg_views WHERE message_id = ?", (mid,))
    row = db.one(ctx.conn, "SELECT status, result_message_id FROM tg_outbox WHERE dedupe_key = 'kit:1'")
    assert tok is not None and tok["tg_message_id"] == mid
    assert view is not None and view["kind"] == "explorer" and view["action_id"] == "x_1" and view["chat_id"] == CHAT
    assert row is not None and row["status"] == "sent" and row["result_message_id"] == mid
    seq = db.one(ctx.conn, "SELECT n FROM tg_thread_seq WHERE thread_key = ?", (f"{CHAT}:0",))
    assert seq is not None and seq["n"] == 1
    with db.transaction(ctx.conn):  # an unbound token never matched; the bound one does now
        ctx.approvals.check_token(token, ("nav",), chat_id=CHAT, message_id=mid)


def test_bad_view_kind_fails_at_enqueue(ctx: AppContext) -> None:
    with pytest.raises(ValueError, match="view kind"):
        ctx.outbox.enqueue("sendMessage", {"chat_id": CHAT, "text": "x"}, view={"kind": "bogus"})
    assert ctx.conn.execute("SELECT COUNT(*) FROM tg_outbox").fetchone()[0] == 0


async def test_skill_part_advances_skill_approvals(ctx: AppContext) -> None:
    install_sources(ctx)
    sid = ctx.decisions.skills.create("sr_1", "git-helper", 1, tg_chat_id=CHAT, body_parts_total=2)
    ctx.outbox.enqueue("sendMessage", {"chat_id": CHAT, "text": "p0"}, dedupe_key=f"part:{sid}:0",
                       approval_id=sid, part_index=0)  # fmt: skip
    await OutboxWorker(ctx.outbox, RecordingSender()).run_once()
    row = db.one(ctx.conn, "SELECT body_parts_delivered FROM skill_approvals WHERE id = ?", (sid,))
    assert row is not None and row["body_parts_delivered"] == 1


async def test_edits_do_not_move_the_thread_counter(ctx: AppContext) -> None:
    ctx.outbox.enqueue("editMessageText", {"chat_id": CHAT, "message_id": 5, "text": "x"})
    await OutboxWorker(ctx.outbox, RecordingSender()).run_once()
    assert ctx.conn.execute("SELECT COUNT(*) FROM tg_thread_seq").fetchone()[0] == 0
