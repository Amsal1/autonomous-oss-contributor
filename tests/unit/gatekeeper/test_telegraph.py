"""Telegraph eligibility, scans and node conversion (DESIGN §14.12.8; AC 27, AC 65, U11)."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from ossbot.gatekeeper import telegraph as tgph
from ossbot.gatekeeper.context import AppContext
from tests.conftest import REPO_ROOT
from tests.telegram.telegraphkit import LONG, FakeTelegraph, enable

GHP = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def _turn(ctx: AppContext, **cols: Any) -> Any:
    values = {"session_key": "tg-4242-1", "thread_id": None, "user_message_id": 1, "status": "done",
              "started_at": "2026-10-20T10:00:00Z", "answer_text": LONG, "tools_json": "[]", "had_attachments": 0,
              "feature_cards": 0, **cols}  # fmt: skip
    cur = ctx.conn.execute(f"INSERT INTO chat_turns ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                           tuple(values.values()))  # fmt: skip
    return tgph.turn_row(ctx, int(cur.lastrowid or 0))


def _eligible(ctx: AppContext, turn: Any) -> str | None:
    return tgph.eligible(turn, ctx.config.telegram.telegraph, None)


def test_off_by_default(ctx: AppContext) -> None:
    assert ctx.config.telegram.telegraph.enabled is False
    assert _eligible(ctx, _turn(ctx)) == "Telegraph is off"


def test_no_other_module_reimplements_an_eligibility_rule() -> None:
    """BA-19: telegraph.eligible is the only check (articles, chat_relay and the task call telegraph.check)."""
    for path in (REPO_ROOT / "src" / "ossbot").rglob("*.py"):
        if path.name in {"telegraph.py", "config.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        for rule in ("offer_min_chars", "max_content_kb", 'turn["feature_cards"]', 'turn["had_attachments"]',
                     "PRIVATE_TOOLS"):  # fmt: skip
            assert rule not in text, (path, rule)


@pytest.mark.parametrize(("cols", "why"), [
    ({}, None),
    ({"answer_text": "short"}, "the answer is short"),
    ({"had_attachments": 1}, "the turn had attachments"),
    ({"tools_json": '["memory"]'}, "the turn used memory, session search or ossbot tools"),
    ({"tools_json": '["web_search", "ossbot_feature_card"]'}, "the turn used memory, session search or ossbot tools"),
    ({"session_key": "tg-4242-reviews"}, "a Reviews answer"),
    ({"feature_cards": 1}, "the turn produced a feature card"),
    ({"status": "failed"}, "no stored answer"),
])  # fmt: skip
def test_eligibility_reads_the_current_row(ctx: AppContext, cols: dict[str, Any], why: str | None) -> None:
    enable(ctx, FakeTelegraph())
    assert _eligible(ctx, _turn(ctx, **cols)) == why


def test_a_reviews_thread_is_never_eligible(ctx: AppContext) -> None:
    enable(ctx, FakeTelegraph())
    turn = _turn(ctx, thread_id=77)
    assert tgph.eligible(turn, ctx.config.telegram.telegraph, 77) == "a Reviews answer"
    assert tgph.eligible(turn, ctx.config.telegram.telegraph, 78) is None


def test_no_token_means_not_offered(ctx: AppContext) -> None:
    enable(ctx, FakeTelegraph())
    turn = _turn(ctx)
    ctx.env = {}
    assert tgph.check(ctx, int(turn["turn_id"])) == "no Telegraph account (ossctl telegraph setup)"


@pytest.mark.parametrize(("extra", "hit"), [
    (f"token {GHP}", "a secret"),
    ("ask test-work-login about it", "a confidential value"),
    ("mail test.person@example.org", "personal details"),
    ("signed, Test Person", "personal details"),
    ("FAKE_LEAK_123", "a secret (gitleaks)"),
])  # fmt: skip
async def test_scans_refuse_with_the_class(ctx: AppContext, extra: str, hit: str) -> None:
    enable(ctx, FakeTelegraph())
    assert await tgph.scan(ctx, LONG) is None
    assert await tgph.scan(ctx, f"{LONG}\n\n{extra}") == hit


def test_nodes_use_only_allowlisted_tags_and_no_media() -> None:
    nodes = tgph.to_nodes(LONG + "\n\n![img](https://x.example/a.png)\n\n> quoted\n\n1. one\n2. two\n\n---\n\n"
                          "[js](javascript:alert(1))")  # fmt: skip
    tags = tgph.tags_of(nodes)
    assert tags <= tgph.ALLOWED_TAGS
    assert {"h3", "p", "ul", "ol", "li", "pre", "code", "blockquote", "b", "a", "hr"} <= tags
    text = json.dumps(nodes)
    assert "javascript:" not in text.replace('"js"', "")  # a non-http(s) link is plain text
    links = [n for n in _walk(nodes) if n.get("tag") == "a"]
    assert all(str(n["attrs"]["href"]).startswith(("https://", "http://")) for n in links)
    assert "| a | b |" in text  # a table becomes pre text


def _walk(nodes: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n in nodes:
        if isinstance(n, dict):
            out.append(n)
            out += _walk(list(n.get("children") or []))
    return out


def test_title_is_the_heading_plus_a_random_suffix() -> None:
    a, b = tgph.make_title(LONG), tgph.make_title(LONG)
    assert a != b
    assert re.fullmatch(r"Terraform state · [A-Z2-7]{8}", a)
    assert re.fullmatch(r".{1,60} · [A-Z2-7]{8}", tgph.make_title("no heading " * 20))
