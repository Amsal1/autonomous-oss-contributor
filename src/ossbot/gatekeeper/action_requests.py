"""One path for requested ``comment`` and ``issue_claim`` actions (DESIGN §12.2-§12.4).

``POST /v1/actions`` (the runner) and the proposal card's ``🙋 Ask maintainers first`` both go through
``request_action``: control must be running, the target must belong to the contribution, the body is
normalized, ends with the hidden ``<!-- ossbot:action=<id> -->`` marker that restart reconciliation
finds (§12.5), and passes ``policy.check_text`` with the gatekeeper's secret values before a row and
its pending approval exist. Nothing is published here.

An ``issue_claim`` requested while another claim of the same contribution is still open returns that
claim instead of a second one, so the runner's fetch stage reuses a claim asked for at gate 1.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ossbot.common.clock import to_iso
from ossbot.common.ids import new_action_id
from ossbot.common.schemas import TERMINAL_STATES, State
from ossbot.gatekeeper import db, policy
from ossbot.gatekeeper.anomaly import Signal
from ossbot.gatekeeper.approvals import normalize_text

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

RequestKind = Literal["issue_claim", "comment"]
OPEN_STATUSES = ("pending_approval", "approved", "executing")
# The same bytes as runner/stages/fetch.py CLAIM_BODY (tests/unit/gatekeeper/test_action_requests.py).
CLAIM_TEMPLATE = (
    "Hi! I'd like to work on this issue and open a pull request with a fix and tests. "
    "Please let me know if someone else is already on it or if you'd prefer a different approach."
    "\n\n— drafted with AI assistance, reviewed by @{login}"
)
SECRET_CODES = frozenset({"secret_pattern", "kiro_key", "gatekeeper_secret"})
OPEN_CLAIM_SQL = (
    "SELECT id FROM outbound_actions WHERE cid = ? AND kind = 'issue_claim' AND status IN (?,?,?)"
    " ORDER BY created_at DESC LIMIT 1"
)


class ActionRefused(RuntimeError):
    def __init__(self, status: int, code: str, violations: list[str] | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.violations = violations or []


@dataclass(frozen=True)
class Requested:
    action_id: str
    approval_id: str | None  # None when an open claim was reused
    reused: bool = False


def claim_body(login: str) -> str:
    return CLAIM_TEMPLATE.format(login=login)


def marked(text: str, action_id: str) -> str:
    return normalize_text(text).rstrip("\n") + f"\n\n<!-- ossbot:action={action_id} -->"


def request_action(ctx: AppContext, cid: str, kind: RequestKind, target_number: int, body: str,
                   actor: str) -> Requested:  # fmt: skip
    """Create the action and its pending approval (the caller publishes the card with
    ``ctx.on_approval_created`` when ``approval_id`` is set). Raises ``control.Blocked`` while paused
    or killed and ``ActionRefused`` otherwise."""
    ctx.control.require_running("request outbound action")
    row = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
    if row is None:
        raise ActionRefused(404, "contribution_not_found")
    if State(row["state"]) in TERMINAL_STATES:
        raise ActionRefused(409, "terminal_contribution")
    allowed = {row["issue_number"]} if kind == "issue_claim" else {row["issue_number"], row["pr_number"]}
    if target_number not in allowed - {None}:
        raise ActionRefused(400, "bad_target")
    if kind == "issue_claim":
        open_claim = db.one(ctx.conn, OPEN_CLAIM_SQL, (cid, *OPEN_STATUSES))
        if open_claim is not None:
            return Requested(str(open_claim["id"]), None, reused=True)
    action_id = new_action_id()
    text = marked(body, action_id)
    violations = policy.check_text(
        "comment", text, ctx.config, policy.effective_confidentiality(ctx.conn, ctx.config), ctx.kiro_key_sha256,
        secret_values=ctx.secret_scanner.values,
    )  # fmt: skip
    if violations:
        if any(v.code in SECRET_CODES for v in violations):
            ctx.anomaly.report(Signal.SECRET_IN_TEXT, f"{kind} for {cid}", cid)
        raise ActionRefused(422, "policy", [v.code for v in violations])
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO outbound_actions (id, cid, kind, payload_json, status, created_at) VALUES (?,?,?,?,?,?)",
            (action_id, cid, kind, json.dumps({"target_number": target_number, "body": text}), "pending_approval",
             to_iso(ctx.clock.now())),
        )  # fmt: skip
        ctx.audit.append(actor, "action_requested", {"action_id": action_id, "kind": kind}, cid)
        approval_id = ctx.create_approval(action_id, actor)
    return Requested(action_id, approval_id)
