"""Shared helpers for the PR review surface tests (PLAN item 45): a delivered card over the fake Bot
API, its derived patch, and lookups of the views the taps produce."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.prefs import UxPrefs
from tests.conftest import packaged_contribution
from tests.telegram.tgharness import TgRig

PATCH = """From 3333333333333333333333333333333333333333 Mon Sep 17 00:00:00 2001
Subject: [PATCH] Fix the thing

---
diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,4 +1,5 @@ def main():
 import os
-x = 1
+x = 2
+y = 3
 print(x)
 print(os.sep)
diff --git a/tests/test_app.py b/tests/test_app.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/tests/test_app.py
@@ -0,0 +1,2 @@
+def test_x():
+    assert True
"""


NUMSTAT = json.dumps([{"added": 2, "deleted": 1, "path": "src/app.py"},
                      {"added": 2, "deleted": 0, "path": "tests/test_app.py"}])  # fmt: skip


@dataclass(frozen=True)
class Card:
    cid: str
    action: str
    approval: str
    message_id: int


def write_patch(ctx: AppContext, cid: str, rev: int, text: str = PATCH) -> None:
    out = ctx.gitops.derived_dir(cid, rev)
    out.mkdir(parents=True, exist_ok=True)
    (out / "change.patch").write_bytes(text.encode("utf-8"))


async def publish(rig: TgRig, action: str) -> Card:
    """A new approval for ``action``, published and delivered (card, parts, then the arm edit). The review
    views stay classic here (``rich_views: off``), so these tests read their HTML; the rich forms and the
    rich -> classic fallback are covered by ``test_rich_fallback.py``."""
    ctx = rig.ctx
    UxPrefs(ctx.conn, ctx.clock).set("rich_views", "off")
    with db.transaction(ctx.conn):
        approval = ctx.create_approval(action, "test")
    ctx.on_approval_created(approval)
    await rig.flush()
    return card_of(ctx, approval)


def card_of(ctx: AppContext, approval: str) -> Card:
    row = db.one(ctx.conn, "SELECT a.tg_message_id, o.id, o.cid FROM approvals a JOIN outbound_actions o"
                 " ON o.id = a.action_id WHERE a.id = ?", (approval,))  # fmt: skip
    assert row is not None and row["tg_message_id"] is not None, approval
    return Card(str(row["cid"]), str(row["id"]), approval, int(row["tg_message_id"]))


async def open_card(rig: TgRig, *, pr_number: int | None = None, patch: str | None = PATCH) -> Card:
    cid, action = packaged_contribution(rig.ctx, pr_number=pr_number)
    if patch is not None:
        write_patch(rig.ctx, cid, 2 if pr_number else 1, patch)
        with db.transaction(rig.ctx.conn):  # packaging's pinned numstat of the same patch
            rig.ctx.conn.execute("UPDATE contributions SET diffstat_json = ? WHERE id = ?", (NUMSTAT, cid))
    return await publish(rig, action)


def newest_approval(ctx: AppContext, action: str) -> str:
    return str(db.one(ctx.conn, "SELECT id FROM approvals WHERE action_id = ? ORDER BY created_at DESC, rowid DESC"
                      " LIMIT 1", (action,))[0])  # fmt: skip


def status(ctx: AppContext, approval: str) -> str:
    return str(db.one(ctx.conn, "SELECT status FROM approvals WHERE id = ?", (approval,))[0])


def latest(rig: TgRig, message_id: int) -> dict[str, Any]:
    """The newest payload that set the text or keyboard of ``message_id`` (an edit, else its send)."""
    for method, params in reversed(rig.req.calls):
        if method.startswith("editMessage") and int(params.get("message_id") or 0) == message_id:
            if method == "editMessageReplyMarkup":
                sent = latest_send(rig, message_id)
                return {**sent, "reply_markup": params.get("reply_markup")}
            return params
    return latest_send(rig, message_id)


def text(rig: TgRig, message_id: int) -> str:
    """The message's newest text with the U+2060 word joiners (path link breakers) removed."""
    return str(latest(rig, message_id).get("text", "")).replace("\u2060", "")


def latest_send(rig: TgRig, message_id: int) -> dict[str, Any]:
    row = db.one(rig.ctx.conn, "SELECT payload_json FROM tg_outbox WHERE result_message_id = ? AND method IN"
                 " ('sendMessage','sendRichMessage') ORDER BY id DESC LIMIT 1", (message_id,))  # fmt: skip
    assert row is not None, message_id
    payload: dict[str, Any] = json.loads(row["payload_json"])
    return payload


def labels(rig: TgRig, message_id: int) -> list[str]:
    return [str(b.get("text", "")) for row in rig.buttons(latest(rig, message_id)) for b in row]


def token(rig: TgRig, message_id: int, label_prefix: str) -> str:
    return rig.token_of(latest(rig, message_id), label_prefix)


def view_message(ctx: AppContext, kind: str, *, stale: bool | None = False) -> int:
    """The newest ``tg_views`` message of ``kind`` (live ones by default)."""
    where = "" if stale is None else f" AND stale = {int(stale)}"
    row = db.one(ctx.conn, f"SELECT message_id FROM tg_views WHERE kind = ?{where} ORDER BY updated_at DESC,"
                 " message_id DESC LIMIT 1", (kind,))  # fmt: skip
    assert row is not None, f"no {kind} view"
    return int(row["message_id"])


def toast(rig: TgRig) -> str:
    return str(rig.req.last("answerCallbackQuery").get("text", ""))


def last_sent_id(rig: TgRig) -> int:
    """The message id of the newest delivered sendMessage (e.g. a ForceReply prompt)."""
    return rig.message_id_of(-1)


def comments(ctx: AppContext, cid: str) -> list[Any]:
    return list(ctx.conn.execute("SELECT * FROM review_comments WHERE cid = ? AND discarded_at IS NULL"
                                 " ORDER BY created_at, rowid", (cid,)).fetchall())  # fmt: skip


def sent_with(rig: TgRig, needle: str) -> int:
    """The message id of the newest delivered sendMessage whose text contains ``needle``."""
    for row in rig.ctx.conn.execute("SELECT result_message_id, payload_json FROM tg_outbox WHERE method ="
                                    " 'sendMessage' AND status = 'sent' ORDER BY id DESC"):  # fmt: skip
        if needle in str(json.loads(row["payload_json"]).get("text", "")):
            return int(row["result_message_id"])
    raise AssertionError(f"no message with {needle!r}")
