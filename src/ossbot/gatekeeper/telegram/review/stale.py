"""S12 stale approvals and S5 "changes requested" (DESIGN §14.11.6, §14.11.12; AC 10).

On every invalidation, expiry or supersession the card is edited within one housekeeping tick
(``CardPublisher.recover`` step 3): a stale header naming the reason, the original header struck
through, a disabled ``⛔ Not approvable`` and, per the table, ``⬇️ Show current`` (only when an open
card exists for the contribution at render time) or ``🔁 Retry``. Its body parts are edited to a
one-line stub so stale verbatim bytes can't be mistaken for current ones; stale view rows get
``tg_views.stale = 1`` so their buttons answer "Superseded…".
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from ossbot.gatekeeper.telegram.escape import esc
from ossbot.gatekeeper.telegram.review.resolve import StaleShowCurrentArg, open_card_exists

Second = Literal["show_current", "retry", "show_or_retry", "none"]
# reason -> (label, second button). Every literal reason an invalidate_*/_mark_invalid call site writes
# has a row (tests/telegram/test_stale.py greps them); prefixes end with ":".
REASON_LABELS: dict[str, tuple[str, Second]] = {
    "expired": ("⌛ Expired · approval was valid 72 h", "retry"),
    "reissued": ("moved to a newer message", "show_current"),
    "text_edited": ("title/body edited", "show_current"),
    "binding_changed": ("content or mode changed", "show_or_retry"),
    "binding_unavailable:": ("derived artifacts unavailable", "none"),
    "live_check:": ("live check failed", "retry"),
    "dry_run_toggled": ("dry-run changed", "show_current"),
    "config_snapshot_changed": ("config reloaded", "show_current"),
    "killed": ("kill", "none"),
    "new_package_rev": ("newer revision", "show_current"),
    "entered_implementing": ("changes requested · new revision in progress", "none"),
    "changes_requested": ("changes requested · new revision in progress", "none"),
    "fix_round_failed": ("fix round failed", "none"),
    "render_failed": ("card couldn't be rendered (bug)", "none"),
    "new_proposal_version": ("newer proposal version", "show_current"),
    "new_skill_version": ("newer skill version staged", "show_current"),
    "tier_changed": ("Hermes tier changed", "retry"),
    "restored": ("restored from a backup", "none"),
    "no_write_token": ("no GitHub write token", "retry"),
    "superseded": ("a newer version was staged", "show_current"),  # skill_reviews: a newer staged skill
}
S5_REASONS = frozenset({"entered_implementing", "changes_requested"})
STALE_BODY_MAX = 3600  # the old card's text kept under the stale header (≤ 3,900 in total)
PART_STUB = "⚪ part of superseded {cid}{rev}"


def label_for(status: str, reason: str | None) -> tuple[str, Second]:
    if status == "expired":
        return REASON_LABELS["expired"]
    code = reason or ""
    if code in REASON_LABELS:
        return REASON_LABELS[code]
    for key, value in REASON_LABELS.items():
        if key.endswith(":") and code.startswith(key):
            detail = code[len(key) :][:40]
            return (f"{value[0]}: {detail}" if detail else value[0]), value[1]
    return f"invalidated ({code[:40]})", "none"


@dataclass(frozen=True)
class StaleRender:
    text: str
    buttons: list[tuple[str, str, dict[str, Any]]]  # (label, purpose, arg) after the disabled one


def header_for(status: str, reason: str | None, *, rev: int | None = None) -> str:
    label, _ = label_for(status, reason)
    if status == "expired":
        return f"⌛ <b>Expired</b> · {esc(label.split(' · ', 1)[-1])}"
    if reason in {"new_package_rev"}:
        return f"⚪ <b>Superseded</b> · r{(rev or 0) + 1} is newer" if rev else "⚪ <b>Superseded</b>"
    if reason == "new_proposal_version":
        return "⚪ <b>Superseded</b> · a newer proposal version exists"
    return f"⚪ <b>Invalidated</b> · {esc(label)}"


def render(conn: sqlite3.Connection, *, card_html: str, status: str, reason: str | None, cid: str | None,
           rev: int | None, action_id: str | None, skill: str | None = None, gate: int | None = None,
           eligible: bool = False) -> StaleRender:  # fmt: skip
    """The S12 card text and its second button (or S5 for a change request)."""
    lines = card_html.split("\n")
    first, rest = lines[0], lines[1:]
    while rest and len(first) + sum(len(x) + 1 for x in rest) > STALE_BODY_MAX:
        rest.pop()  # whole lines from the end: the stale card keeps its head (no cut inside a tag)
    if reason in S5_REASONS:
        head = f"✏️ <b>Changes requested</b> · {esc(cid or '')}" + (f" · r{rev} → r{rev + 1} in progress" if rev else "")
        return StaleRender("\n".join([head, f"<s>{first}</s>", *rest]), [])
    head = header_for(status, reason, rev=rev)
    _, second = label_for(status, reason)
    buttons: list[tuple[str, str, dict[str, Any]]] = []
    show_arg: dict[str, Any] = {"skill": skill} if skill else ({"cid": cid, "gate": 1} if gate == 1 else {"cid": cid})
    can_show = (skill or cid) is not None and open_card_exists(conn, StaleShowCurrentArg.model_validate(show_arg))
    retry_arg = {"cid": cid, "rev": rev, "action_id": action_id}
    if second in {"show_current", "show_or_retry"} and can_show:
        buttons.append(("⬇️ Show current", "stale_show_current", show_arg))
    elif second in {"retry", "show_or_retry", "show_current"} and eligible and cid is not None:
        buttons.append(("🔁 Retry", "stale_retry", retry_arg))
    return StaleRender("\n".join([head, f"<s>{first}</s>", *rest]), buttons)


def part_stub(cid: str | None, rev: int | None) -> str:
    return esc(PART_STUB.format(cid=cid or "", rev=f" r{rev}" if rev else ""))


def stale_views(conn: sqlite3.Connection, approval_id: str) -> list[sqlite3.Row]:
    """The approval's part messages (body_part, commit_msg, skill_part) recorded at send time."""
    return conn.execute(
        "SELECT * FROM tg_views WHERE approval_id = ? AND kind IN ('body_part','commit_msg','skill_part')"
        " AND stale = 0", (approval_id,),
    ).fetchall()  # fmt: skip


def rev_of(payload_json: str | None) -> int | None:
    try:
        rev = json.loads(payload_json or "{}").get("rev")
    except ValueError:
        return None
    return rev if isinstance(rev, int) else None


def mark_action_views_stale(conn: sqlite3.Connection, action_id: str) -> None:
    """Explorer/file/interdiff views of a superseded rev answer "Superseded…" from now on."""
    conn.execute("UPDATE tg_views SET stale = 1 WHERE action_id = ? AND kind IN ('explorer','file','interdiff',"
                 "'draft_review')", (action_id,))  # fmt: skip
