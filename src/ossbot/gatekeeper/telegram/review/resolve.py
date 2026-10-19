"""Kit-token resolution by ``action_id`` with the purpose-class outcomes (DESIGN §14.18.2, delta 1).

Every review kit token carries ``{cid, rev, action_id}`` and no approval id, so Cancel, ``reissue()``
and invalidations never kill navigation, comment prompts or the draft review. At tap time the
action's current open approval is looked up; the outcome depends on the purpose class:

* navigation works while an approval is open, and read-only once the action was submitted;
* comment and send purposes answer "Submitted: comment on the PR instead." after a submit;
* everything else on a dead action answers "Superseded…".

``stale_show_current`` resolves by contribution (or skill), never by action (§14.11.12, BA-11).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ossbot.common.ids import ID_RE
from ossbot.gatekeeper import db

SUPERSEDED = "Superseded: content or mode changed since this card; review the new card."
SUBMITTED = "Submitted: comment on the PR instead."
DRY_RUN_DONE = "Dry run done: this rev was not published."
SUBMITTED_STATUSES = frozenset({"approved", "executing", "done", "dry_run_done"})
PurposeClass = Literal["navigation", "comment"]
CID_PATTERN = r"^[a-z]_[0-9A-HJKMNP-TV-Z]{10}$"  # common.ids.ID_RE (pydantic's regex engine has no \Z)
assert ID_RE.pattern.removesuffix(r"\Z") == CID_PATTERN.removesuffix("$")
SKILL_PATTERN = r"^[a-z0-9-]{1,64}$"


class ReviewArg(BaseModel):
    """The arg of every review kit token (§14.20.6): ``cid``, ``rev`` and ``action_id`` (an ``x_`` id, or
    ``None`` only for read-only explorers), plus per-purpose fields."""

    model_config = ConfigDict(extra="forbid")
    cid: str = Field(pattern=CID_PATTERN)
    rev: int | None = Field(default=None, ge=1)
    action_id: str | None = Field(default=None, pattern=r"^x_[0-9A-HJKMNP-TV-Z]{10}$")
    view: str | None = Field(default=None, pattern=r"^[a-z_]{1,20}$")
    section: int | None = Field(default=None, ge=1, le=9)
    page: int | None = Field(default=None, ge=1, le=10_000)
    path: str | None = Field(default=None, max_length=1000)
    index: int | None = Field(default=None, ge=0, le=100_000)
    hunk: int | None = Field(default=None, ge=0, le=100_000)
    comment_id: str | None = Field(default=None, max_length=40)
    choice: str | None = Field(default=None, max_length=40)
    side: Literal["old", "new"] | None = None
    line: int | None = Field(default=None, ge=1, le=10_000_000)
    text: str | None = Field(default=None, min_length=1, max_length=4000)  # a pending comment (line picker)
    message_id: int | None = Field(default=None, ge=1)


class StaleShowCurrentArg(BaseModel):
    """BA-11: exactly ``{cid}``, ``{cid, gate: 1}`` or ``{skill}``."""

    model_config = ConfigDict(extra="forbid")
    cid: str | None = Field(default=None, pattern=CID_PATTERN)
    gate: Literal[1] | None = None
    skill: str | None = Field(default=None, pattern=SKILL_PATTERN)

    @model_validator(mode="after")
    def _shape(self) -> StaleShowCurrentArg:
        if self.skill is not None:
            if self.cid is not None or self.gate is not None:
                raise ValueError("{skill} takes no other field")
        elif self.cid is None:
            raise ValueError("cid or skill required")
        return self


@dataclass(frozen=True)
class Resolved:
    approval_id: str | None  # the open (pending/armed) approval of the action, if any
    approval_status: str | None
    action_status: str | None
    kind: str | None

    @property
    def open(self) -> bool:
        return self.approval_id is not None

    @property
    def submitted(self) -> bool:
        return self.action_status in SUBMITTED_STATUSES


def resolve(conn: sqlite3.Connection, action_id: str | None) -> Resolved:
    if action_id is None:
        return Resolved(None, None, None, None)
    row = db.one(conn, "SELECT a.id, a.status FROM approvals a WHERE a.action_id = ? AND a.status IN"
                 " ('pending','armed') ORDER BY a.created_at DESC LIMIT 1", (action_id,))  # fmt: skip
    action = db.one(conn, "SELECT status, kind FROM outbound_actions WHERE id = ?", (action_id,))
    return Resolved(
        str(row["id"]) if row else None, str(row["status"]) if row else None,
        str(action["status"]) if action else None, str(action["kind"]) if action else None,
    )  # fmt: skip


def refusal(conn: sqlite3.Connection, action_id: str | None, purpose_class: PurposeClass,
            *, view_stale: bool = False) -> str | None:  # fmt: skip
    """None = the tap may act (navigation may be read-only); else the answer text (§14.18.2 table)."""
    if view_stale:
        return SUPERSEDED
    if action_id is None:  # read-only explorers (rescope/ceiling): navigation only
        return None if purpose_class == "navigation" else SUPERSEDED
    res = resolve(conn, action_id)
    if res.open:
        return None
    if res.submitted:
        if purpose_class == "navigation":
            return None
        return DRY_RUN_DONE if res.action_status == "dry_run_done" else SUBMITTED
    return SUPERSEDED


def open_card_exists(conn: sqlite3.Connection, arg: StaleShowCurrentArg) -> bool:
    """S12 renders ``⬇️ Show current`` only when this is true at render time (BA-11)."""
    if arg.skill is not None:
        return db.one(conn, "SELECT 1 FROM skill_approvals WHERE skill = ? AND status IN ('pending','armed')",
                      (arg.skill,)) is not None  # fmt: skip
    pr = db.one(conn, "SELECT 1 FROM approvals a JOIN outbound_actions o ON o.id = a.action_id WHERE o.cid = ?"
                " AND a.status IN ('pending','armed') LIMIT 1", (arg.cid,))  # fmt: skip
    proposal = db.one(conn, "SELECT 1 FROM proposal_approvals WHERE cid = ? AND status IN ('pending','armed')",
                      (arg.cid,))  # fmt: skip
    return pr is not None or proposal is not None


def view_is_stale(conn: sqlite3.Connection, message_id: int) -> bool:
    row = db.one(conn, "SELECT stale FROM tg_views WHERE message_id = ?", (message_id,))
    return bool(row["stale"]) if row is not None else False


def state_of(conn: sqlite3.Connection, cid: str) -> str:
    row = db.one(conn, "SELECT state FROM contributions WHERE id = ?", (cid,))
    return str(row["state"]) if row else "unknown"


_SAFE = re.compile(r"[^A-Za-z0-9_ -]")


def nothing_text(state: str) -> str:
    return f"Nothing to re-send (state {_SAFE.sub('', state)[:30]})"
