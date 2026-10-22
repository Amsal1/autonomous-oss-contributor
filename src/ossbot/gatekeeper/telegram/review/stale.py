"""S12 stale approvals and S5 "changes requested" (DESIGN §14.11.6, §14.11.12; AC 10).

On every invalidation, expiry or supersession the card is edited within one housekeeping tick
(``CardPublisher.recover`` step 3): a stale header naming the reason and the subject in copyable form
(``ui/ids.py``), the original header struck through, a disabled ``⛔ Not approvable`` and the way
forward (``CardPublisher._forward``): ``⬇️ Show current`` when an open card exists or a re-send is owed
(``auto_reissue``), ``🔁 Retry`` for the explicit-retry rows (expiry, a failed live check, no write
token) while the action is eligible, else no second button and one reason line ("Nothing to approve:
state X"). Its body parts are edited to a one-line stub so stale verbatim bytes can't be mistaken for
current ones; stale view rows get ``tg_views.stale = 1`` so their buttons answer "Superseded…".
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from ossbot.gatekeeper.telegram.escape import esc
from ossbot.gatekeeper.telegram.ui import ids

Second = Literal["show_current", "retry", "show_or_retry", "none"]
Forward = Literal["show_current", "retry", "none"]
# reason -> (label, second button). Every literal reason an invalidate_*/_mark_invalid call site writes
# has a row (tests/telegram/test_stale.py greps them); prefixes end with ":".
REASON_LABELS: dict[str, tuple[str, Second]] = {
    "expired": ("⌛ Expired · approval was valid 72 h", "retry"),
    "reissued": ("moved to a newer message", "show_current"),
    "text_edited": ("title/body edited", "show_current"),
    "binding_changed": ("content or mode changed", "show_current"),
    "binding_unavailable:": ("derived artifacts unavailable", "none"),
    "live_check:": ("live check failed", "retry"),
    "dry_run_toggled": ("dry-run changed", "show_current"),
    "config_snapshot_changed": ("config reloaded", "show_current"),
    "killed": ("kill", "show_current"),  # re-sent after resume (auto_reissue)
    "new_package_rev": ("newer revision", "show_current"),
    "entered_implementing": ("changes requested · new revision in progress", "none"),
    "changes_requested": ("changes requested · new revision in progress", "none"),
    "fix_round_failed": ("fix round failed", "none"),
    "render_failed": ("card couldn't be rendered (bug)", "none"),
    "new_proposal_version": ("newer proposal version", "show_current"),
    "new_skill_version": ("newer skill version staged", "show_current"),
    "tier_changed": ("Hermes tier changed", "show_current"),
    "restored": ("restored from a backup", "none"),
    "no_write_token": ("no GitHub write token", "retry"),
    "superseded": ("a newer version was staged", "show_current"),  # skill_reviews: a newer staged skill
    # open-discovery §13.3: no Retry/Show current while held (Resume on the hold card first)
    "egress_anomaly": ("egress anomaly: contribution held", "none"),
}
S5_REASONS = frozenset({"entered_implementing", "changes_requested"})
# the rows whose way forward is an explicit 🔁 Retry (a fresh window), not an automatic re-send
RETRY_REASONS = frozenset({"no_write_token"})
RETRY_PREFIXES = ("live_check:",)
STALE_BODY_MAX = 3600  # the old card's text kept under the stale header
STALE_MAX = 3900  # the whole stale card (head, reason line, struck-through body)
HELD_LINE = "Held: egress anomaly · Resume on the hold card"
SKILL_REOPEN = "Still staged: open it again from /queue"
REOPEN = "Still waiting: open it again from /queue"
DEFERRED = "Paused or killed: the fresh card follows after /resume"
_SAFE = re.compile(r"[^A-Za-z0-9_ -]")
PART_STUB = "⚪ part of superseded {cid}{rev}"
PROPOSAL_EXPIRED_LABEL = "re-scouting if still relevant"
NOT_APPROVABLE = "⛔ Not approvable"
NOT_DECIDABLE = "⛔ Not decidable"  # gate 1 (proposal) cards, §14.11.15-§14.11.16


def disabled_label(approval_id: str) -> str:
    return NOT_DECIDABLE if approval_id.startswith("p_") else NOT_APPROVABLE


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


def header_for(status: str, reason: str | None, *, rev: int | None = None, gate: int | None = None) -> str:
    label, _ = label_for(status, reason)
    if status == "expired" and gate == 1:
        return f"⌛ <b>Expired</b> · {PROPOSAL_EXPIRED_LABEL}"  # §14.11.15 Expiry
    if status == "expired":
        return f"⌛ <b>Expired</b> · {esc(label.split(' · ', 1)[-1])}"
    if reason in {"new_package_rev"}:
        return f"⚪ <b>Superseded</b> · r{(rev or 0) + 1} is newer" if rev else "⚪ <b>Superseded</b>"
    if reason == "new_proposal_version":
        return f"⚪ <b>Superseded</b> · v{rev + 1} is newer" if rev else "⚪ <b>Superseded</b> · a newer version exists"
    return f"⚪ <b>Invalidated</b> · {esc(label)}"


def is_retry_row(status: str, reason: str | None) -> bool:
    """Expiry, a failed live check, no write token: the owner restarts the window with 🔁 Retry."""
    code = reason or ""
    return status == "expired" or code in RETRY_REASONS or code.startswith(RETRY_PREFIXES)


def show_arg(cid: str | None, skill: str | None, gate: int | None) -> dict[str, Any]:
    """BA-11: exactly ``{cid}``, ``{cid, gate: 1}`` or ``{skill}``."""
    return {"skill": skill} if skill else ({"cid": cid, "gate": 1} if gate == 1 else {"cid": cid})


def safe_state(state: str) -> str:
    return _SAFE.sub("", state)[:30]


def nothing_line(*, state: str | None = None, skill: str | None = None, gate: int | None = None,
                 held: bool = False) -> str:  # fmt: skip
    """The one reason line of a stale card (and the Show current toast) when nothing is eligible."""
    if held:
        return HELD_LINE
    if skill is not None:
        return "Nothing to decide: skill no longer staged"
    verb = "decide" if gate == 1 else "approve"
    return f"Nothing to {verb}: state {safe_state(state or 'unknown')}"


def subject_html(cid: str | None, skill: str | None) -> str:
    """The subject in copyable form for the stale header (D9)."""
    if skill:
        return ids.html(skill, "skill")
    return ids.html(cid, "cid") if cid else ""


def _assemble(head: str, card_html: str, reason_line: str | None = None) -> str:
    lines = card_html.split("\n")
    first, rest = lines[0], lines[1:]
    extra = [esc(reason_line)] if reason_line else []
    fixed = len(head) + sum(len(x) + 1 for x in extra) + len(first) + len("<s></s>") + 1

    def body() -> int:
        return sum(len(x) + 1 for x in rest)

    while rest and (len(first) + body() > STALE_BODY_MAX or fixed + body() > STALE_MAX):
        rest.pop()  # whole lines from the end: the stale card keeps its head (no cut inside a tag)
    return "\n".join([head, *extra, f"<s>{first}</s>", *rest])


def render(*, card_html: str, status: str, reason: str | None, cid: str | None, rev: int | None,
           action_id: str | None, skill: str | None = None, gate: int | None = None, forward: Forward = "none",
           reason_line: str | None = None) -> StaleRender:  # fmt: skip
    """The S12 card text and its second button (or S5 for a change request). ``forward`` and
    ``reason_line`` come from ``CardPublisher._forward``; the reason line is shown only without a button."""
    subject = subject_html(cid, skill)
    if reason in S5_REASONS:
        head = f"✏️ <b>Changes requested</b> · {subject}" + (f" · r{rev} → r{rev + 1} in progress" if rev else "")
        return StaleRender(_assemble(head, card_html), [])
    head = header_for(status, reason, rev=rev, gate=gate) + (f" · {subject}" if subject else "")
    if status == "expired" and gate == 1:  # re-scouted when still relevant: nothing to tap (§14.11.15)
        return StaleRender(_assemble(head, card_html), [])
    buttons: list[tuple[str, str, dict[str, Any]]] = []
    if forward == "show_current" and (cid or skill):
        buttons.append(("⬇️ Show current", "stale_show_current", show_arg(cid, skill, gate)))
    elif forward == "retry" and cid is not None:
        buttons.append(("🔁 Retry", "stale_retry", {"cid": cid, "rev": rev, "action_id": action_id}))
    return StaleRender(_assemble(head, card_html, None if buttons else reason_line), buttons)


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
