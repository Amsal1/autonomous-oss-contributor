"""🔁 Retry of a failed submission (DESIGN §14.11.9): one function for the retry note's button and
``/queue`` -> Open on a ``SUBMIT_FAILED`` item.

A retry never approves anything: it creates a new ``pending_approval`` action for the same content and a
fresh approval (the full two-tap approval on the fresh card again, I1, I3). Runs inside the caller's
transaction (``db.transaction`` nests); the caller publishes the returned approval after the commit.
"""

from __future__ import annotations

import json
from typing import Any

from ossbot.common.clock import to_iso
from ossbot.common.ids import new_action_id
from ossbot.common.schemas import State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalError
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.state_machine import Actor, TransitionError

MARKER_PREFIX = "<!-- ossbot:action="


def retry_failed(ctx: AppContext, action_id: str, actor: str) -> str:
    """The new approval id for a fresh copy of the failed action ``action_id``; ``ApprovalError``
    (``retry_unavailable``) when it is not a failed action that may be retried."""
    with db.transaction(ctx.conn):
        old = db.one(ctx.conn, "SELECT * FROM outbound_actions WHERE id = ?", (action_id,))
        if old is None or old["status"] != "failed":
            raise ApprovalError("retry_unavailable")
        contrib = db.one(ctx.conn, "SELECT state FROM contributions WHERE id = ?", (old["cid"],))
        if contrib is None:
            raise ApprovalError("retry_unavailable")
        payload: dict[str, Any] = json.loads(old["payload_json"])
        new_id = new_action_id()
        if old["kind"] in {"comment", "issue_claim"}:
            body = str(payload["body"])
            payload["body"] = body.replace(f"{MARKER_PREFIX}{old['id']} -->", f"{MARKER_PREFIX}{new_id} -->")
        elif contrib["state"] == State.SUBMIT_FAILED.value:
            try:
                ctx.state_machine.transition(old["cid"], State.SUBMIT_FAILED, State.AWAITING_APPROVAL, Actor.HUMAN,
                                             {}, actor)  # fmt: skip
            except TransitionError as exc:
                raise ApprovalError("retry_unavailable", exc.code) from exc
        else:
            raise ApprovalError("retry_unavailable", contrib["state"])
        ctx.conn.execute(
            "INSERT INTO outbound_actions (id, cid, kind, payload_json, status, created_at) VALUES (?,?,?,?,?,?)",
            (new_id, old["cid"], old["kind"], json.dumps(payload, sort_keys=True), "pending_approval",
             to_iso(ctx.clock.now())),
        )  # fmt: skip
        approval_id = ctx.create_approval(new_id, actor)
        ctx.audit.append(actor, "retry_requested", {"old_action": old["id"], "action_id": new_id}, old["cid"])
    return approval_id
