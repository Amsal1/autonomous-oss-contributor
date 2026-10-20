"""BA-3 (DESIGN §14.18.2): dedupe keys are permanent, so a key is reused only for what happens once
ever (card, part, first arm); repeatable edits carry a per-occurrence component."""

from __future__ import annotations

import ast
import re

from ossbot.gatekeeper.context import AppContext
from tests.conftest import REPO_ROOT
from tests.unit.gatekeeper.decision_helpers import CHAT

SRC = REPO_ROOT / "src" / "ossbot" / "gatekeeper"
# key format -> True when it may repeat for the same subject (needs a per-occurrence component)
KEY_FORMATS: dict[str, bool] = {
    "card:{approval_id}": False,
    "part:{approval_id}:{index}": False,
    "arm:{approval_id}": False,
    "retry:{action_id}": False,
    "retry-arm:{action_id}": False,
    "disarm:{approval_id}:{armed_at}": True,
    # the rest of BA-3 (FEAT-009): stale cards, comment-refresh re-renders, stubs and keyless kit views
    "stale:{approval_id}": False,
    "stale:{approval_id}:{comment_id}": True,
    "stale:{approval_id}:swept": False,
    "s1:{approval_id}:{comment_id}": True,
    "stub:{message_id}:{reason}": True,
    "stub:{message_id}:moved": False,  # the "moved ↓" stub of a view re-sent after a failed rich edit
    "stub:{message_id}:delete": False,  # ... deleted when that stub edit failed too
    "stub:{message_id}:submitted": False,  # S9: the card's one outcome edit after the submit returns
    "stub:{message_id}:rejected": False,  # /reject: the card's one outcome edit (an approval rejects once)
    "view:{token}": False,  # new per send (a fresh token), so it never repeats
    # FEAT-010
    "react:{message_id}:{emoji}": False,  # one reaction per message and emoji (item 49)
    "digest:{kind}:{period_start}": False,  # one digest per kind and period (item 50)
    "status:{cid}:card": False,  # the status card is sent once per PR
    "status:{cid}:merged": False,  # the merged summary once per PR
    "status:{cid}:{event_id}": False,  # one P1 reply per GitHub event id
    "status:{cid}:{content_hash}:{edited_at}": True,  # a status card edit (content can recur)
    "mirror_row4:{approval_id}": False,  # card row 4 once per approval (item 51)
}


def _dedupe_keys_in_source() -> set[str]:
    """Every f-string passed as ``dedupe_key=`` in the gatekeeper, normalized to the format above."""
    out: set[str] = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "dedupe_key" and isinstance(node.value, ast.JoinedStr):
                text = ast.unparse(node.value)[2:-1]  # f'...'
                text = text.replace("{CARD_PREFIX}", "card:").replace("{RETRY_PREFIX}", "retry:")
                out.add(re.sub(r"\{[^}]*\}", "{}", text))
    return out


def test_every_dedupe_key_format_is_listed() -> None:
    listed = {re.sub(r"\{[^}]*\}", "{}", k) for k in KEY_FORMATS}
    assert _dedupe_keys_in_source() <= listed, _dedupe_keys_in_source() - listed


def test_repeatable_keys_carry_an_occurrence() -> None:
    for fmt, repeats in KEY_FORMATS.items():
        if repeats:
            assert fmt.count("{") >= 2, fmt


def test_duplicate_key_is_a_noop(ctx: AppContext) -> None:
    first = ctx.outbox.enqueue("sendMessage", {"chat_id": CHAT, "text": "a"}, dedupe_key="card:a_X")
    again = ctx.outbox.enqueue("sendMessage", {"chat_id": CHAT, "text": "b"}, dedupe_key="card:a_X")
    assert first is not None and again is None
    assert ctx.conn.execute("SELECT COUNT(*) FROM tg_outbox").fetchone()[0] == 1
