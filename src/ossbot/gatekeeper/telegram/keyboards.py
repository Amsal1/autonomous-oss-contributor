"""Inline keyboards. Every button carries an opaque ``t:<22-char token>`` (24 bytes, §14.3)."""

from __future__ import annotations

import re
from typing import Any

from ossbot.common.schemas import OutboundKind

CALLBACK_RE = re.compile(r"^t:[A-Za-z0-9_-]{22}\Z")
Rows = list[list[tuple[str, str]]]  # rows of (label, token)

APPROVE_LABEL: dict[OutboundKind, str] = {
    "open_pr": "✅ Approve & Submit",
    "push_update": "✅ Approve & Push",
    "comment": "✅ Approve & Post",
    "issue_claim": "✅ Approve & Post",
}


def callback_data(token: str) -> str:
    data = f"t:{token}"
    if not CALLBACK_RE.match(data):
        raise ValueError("bad token")
    return data


def token_of(data: str | None) -> str | None:
    if data is None or not CALLBACK_RE.match(data):
        return None
    return data[2:]


def card_rows(kind: OutboundKind, tokens: dict[str, str]) -> Rows:
    """Phase-1 minimal card keyboard (Decision 7); the S1 card rows arrive in PLAN item 45 (FEAT-009)."""
    return [[(APPROVE_LABEL[kind], tokens["approve"]), ("❌ Reject", tokens["reject"])]]


def confirm_rows(confirm: str, cancel: str) -> Rows:
    return [[("✅ Confirm", confirm), ("↩️ Cancel", cancel)]]


def single_row(label: str, token: str) -> Rows:
    return [[(label, token)]]


def to_json(rows: Rows) -> dict[str, Any]:
    """``reply_markup`` for raw Bot API payloads (outbox)."""
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": callback_data(token)} for label, token in row] for row in rows
        ]
    }
