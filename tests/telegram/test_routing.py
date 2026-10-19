"""The update routing table (DESIGN §14.10.4, normative): every rule, in order, plus stale messages."""

from __future__ import annotations

from typing import Any

import pytest

from ossbot.common.clock import to_iso
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.routing import Incoming, classify
from ossbot.gatekeeper.telegram.runtime import ui_of

OWNER = 4242


def _inc(**kw: Any) -> Incoming:
    return Incoming(kind=kw.pop("kind", "message"), chat_id=OWNER, **kw)


def _topics(ctx: AppContext) -> None:
    topics = ui_of(ctx).topics
    with db.transaction(ctx.conn):
        for name, tid in (("control", 11), ("reviews", 12), ("feed", 13), ("assistant", 14)):
            ctx.conn.execute("INSERT INTO tg_topics (name, thread_id, created_at) VALUES (?, ?, 'x')", (name, tid))
    topics.mode = "topics"


def _view(ctx: AppContext, message_id: int, kind: str) -> None:
    with db.transaction(ctx.conn):
        ctx.conn.execute("INSERT INTO tg_views (message_id, chat_id, kind, updated_at) VALUES (?, ?, ?, ?)",
                         (message_id, OWNER, kind, to_iso(ctx.clock.now())))  # fmt: skip


def _route(ctx: AppContext, inc: Incoming) -> Any:
    ui = ui_of(ctx)
    return classify(ctx.conn, ui.topics, ui.pending, inc)


def test_rules_1_and_2(ctx: AppContext) -> None:
    assert _route(ctx, _inc(kind="callback")).kind == "callback"
    assert _route(ctx, _inc(kind="stopped")).kind == "stop_generation"


def test_rule_3_prompt_wins_over_commands(ctx: AppContext) -> None:
    ui_of(ctx).pending.open("comment", chat_id=OWNER, prompt_message_id=500)
    route = _route(ctx, _inc(text="/api/v1 is wrong", reply_to_message_id=500))
    assert route.kind == "pending_input" and route.prompt is not None and route.prompt.kind == "comment"
    assert _route(ctx, _inc(text="/api/v1 is wrong", reply_to_message_id=501)).kind == "command"


def test_rule_4_commands_in_any_topic(ctx: AppContext) -> None:
    _topics(ctx)
    for thread in (None, 11, 12, 13, 14, 99):
        assert _route(ctx, _inc(text="/status", thread_id=thread)).kind == "command"


@pytest.mark.parametrize("kind", ["file", "explorer", "interdiff", "card", "body_part", "commit_msg", "proposal",
                                  "skill_card", "skill_part"])  # fmt: skip
def test_rule_5_replies_to_views_resolve_through_tg_views(ctx: AppContext, kind: str) -> None:
    _view(ctx, 600, kind)
    route = _route(ctx, _inc(text="this line is wrong", reply_to_message_id=600))
    assert route.kind == "view_reply" and route.view is not None and route.view["kind"] == kind


def test_rule_5_ignores_other_views_and_unknown_messages(ctx: AppContext) -> None:
    _view(ctx, 601, "fyi")
    assert _route(ctx, _inc(text="hello", reply_to_message_id=601)).kind == "chat"
    assert _route(ctx, _inc(text="hello", reply_to_message_id=999)).kind == "chat"


def test_rule_6_links(ctx: AppContext) -> None:
    only = _route(ctx, _inc(text="https://github.com/langfuse/langfuse/issues/12"))
    assert only.kind == "link_card" and only.then is None and only.links[0].number == 12
    more = _route(ctx, _inc(text="github.com/a/b/pull/3 keep it minimal"))
    assert more.kind == "link_card" and more.then == "chat" and more.notes == "keep it minimal"
    fwd = _route(ctx, _inc(text="github.com/a/b look", forwarded=True))
    assert fwd.notes == "" and fwd.then == "chat"
    many = _route(ctx, _inc(text=" ".join(f"https://github.com/o/r/issues/{n}" for n in range(1, 6))))
    assert len(many.links) == 3
    assert _route(ctx, _inc(text="http://github.com/a/b")).kind == "chat"  # http:// is not accepted
    assert _route(ctx, _inc(text="see https://gitlab.com/a/b")).kind == "chat"
    assert _route(ctx, _inc(text="https://github.com/a/b", reply_to_message_id=5)).kind == "chat"  # replies


def test_rules_7_8_9_by_topic(ctx: AppContext) -> None:
    _topics(ctx)
    assert _route(ctx, _inc(text="hi", thread_id=12)).kind == "reviews_chat"
    assert _route(ctx, _inc(text="hi", thread_id=11)).kind == "system_chat"
    assert _route(ctx, _inc(text="hi", thread_id=13)).kind == "system_chat"
    assert _route(ctx, _inc(text="hi", thread_id=14)).kind == "chat"
    assert _route(ctx, _inc(text="hi", thread_id=77)).kind == "chat"  # a user topic
    assert _route(ctx, _inc(text="", has_media=True, thread_id=14)).kind == "chat"
    assert _route(ctx, _inc(text="  ")).kind == "ignore"


def test_flat_mode_has_no_system_topics(ctx: AppContext) -> None:
    _topics(ctx)
    ui_of(ctx).topics.mode = "flat"
    assert _route(ctx, _inc(text="hi", thread_id=12)).kind == "chat"
