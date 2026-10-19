"""The gatekeeper half of the skill gate (DESIGN §15.5, §14.15; PLAN item 36).

The root hasher (``ossbot.ctl.skills_root scan``) lists every skill Hermes staged, with its file
hashes, declared tools, tier and diff, in ``/var/lib/ossbot/skills/pending.json`` (root:gatekeeper
0640). ``SkillReviews.sync`` (housekeeping tick) imports it into ``skill_reviews``, supersedes older
staged versions of a skill, and opens a two-tap ``SkillApprovals`` card for each new review. The
approval binds the content digest, the file hashes, the declared tools and the tier (``DbSkillSource``);
a review staged under another tier, or one declaring tools above the active tier, cannot be approved.
After Confirm and ``consume``, ``request_activation`` writes ``control/skill-request-<approval>.json``;
the root path unit copies exactly the bound bytes into the read-only active dir and records the result
in ``decisions.json``, which the next sync imports. Hermes output is data: every field is validated.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ossbot.common.clock import Clock, to_iso
from ossbot.common.control_flags import HermesTier, write_flag
from ossbot.common.paths import Paths
from ossbot.gatekeeper import db
from ossbot.gatekeeper.approvals import ApprovalError
from ossbot.gatekeeper.audit import AuditLog
from ossbot.gatekeeper.chat.hermes_surface import PLUGIN_PREFIX, allowed_tools
from ossbot.gatekeeper.skill_approvals import SkillApprovals
from ossbot.gatekeeper.telegram.cards import CardMessages
from ossbot.gatekeeper.telegram.escape import b, esc
from ossbot.gatekeeper.telegram.ui.redact import redact

log = logging.getLogger(__name__)

MAX_INDEX = 8 * 1024 * 1024
PART_CHARS = 3500
MAX_PARTS = 5
SHA = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
# Kiro session tools are on in every tier (§15.5); a skill may name them without asking for more.
KIRO_TOOLS = frozenset({"web_search", "web_fetch"})


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_id: Annotated[str, Field(pattern=r"^r_[0-9a-f]{20}$")]
    pending_id: Annotated[str, Field(pattern=r"^[0-9a-f]{6,32}$")]
    skill: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")]
    rel_dir: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}(/[a-z0-9][a-z0-9_.-]{0,63})?$")]
    action: Literal["create", "update", "delete"]
    files: dict[Annotated[str, Field(max_length=200)], SHA] = Field(max_length=50)
    content_sha256: SHA
    declared_tools: list[Annotated[str, Field(max_length=64)]] = Field(max_length=50)
    tier: Literal["locked", "research", "self_improve"]
    summary: Annotated[str, Field(max_length=300)]
    origin: Annotated[str, Field(max_length=40)]
    created_at: Annotated[str, Field(max_length=40)]
    diff: Annotated[str, Field(max_length=70000)]


class Decision(BaseModel):
    model_config = ConfigDict(extra="ignore")
    review_id: str | None = None
    result: Literal["activated", "rejected", "failed"]
    reason: str | None = None


def _read_json(path: Path) -> Any:
    """A root-written file, read with ``O_NOFOLLOW`` and a size limit; None when absent or broken."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            return None
        data = fh.read(MAX_INDEX + 1)
    if len(data) > MAX_INDEX:
        return None
    try:
        return json.loads(data)
    except ValueError:
        return None


def files_digest(files_json: str) -> str:
    return hashlib.sha256(files_json.encode()).hexdigest()


def tools_above(tier: HermesTier, declared: list[str]) -> list[str]:
    ok = allowed_tools(tier) | KIRO_TOOLS
    return sorted(t for t in declared if t not in ok and not t.startswith(PLUGIN_PREFIX))


class DbSkillSource:
    """``SkillSource`` over ``skill_reviews``: the binding is recomputed from the stored review."""

    def __init__(self, tier: Callable[[], HermesTier]) -> None:
        self.tier = tier

    def fields(self, conn: sqlite3.Connection, skill_review_id: str) -> dict[str, Any]:
        row = db.one(conn, "SELECT * FROM skill_reviews WHERE id = ?", (skill_review_id,))
        if row is None or row["status"] != "staged":
            raise ApprovalError("skill_unavailable", skill_review_id)
        tier = self.tier()
        if row["tier"] != tier:
            raise ApprovalError("tier_changed", f"staged under {row['tier']}, active tier {tier}")
        declared = json.loads(row["declared_tools_json"])
        above = tools_above(tier, declared)
        if above:
            raise ApprovalError("tier_above", ", ".join(above))
        return {
            "content_sha256": row["content_sha256"],
            "files_sha256": files_digest(row["files_json"]),
            "declared_tools": ",".join(declared) or "none",
            "tier": tier,
            "action": row["action"],
        }

    def reissuable(self, conn: sqlite3.Connection, skill: str) -> str | None:
        row = db.one(
            conn,
            "SELECT * FROM skill_reviews WHERE skill = ? AND status = 'staged' ORDER BY created_at DESC LIMIT 1",
            (skill,),
        )
        if (
            row is None
            or row["tier"] != self.tier()
            or tools_above(self.tier(), json.loads(row["declared_tools_json"]))
        ):
            return None
        return str(row["id"])


def render_skill_card(conn: sqlite3.Connection, row: sqlite3.Row) -> CardMessages:
    """The minimal skill card (§14.15; the kit renderer is PLAN item 47): escaped text, diff as parts."""
    review = db.one(conn, "SELECT * FROM skill_reviews WHERE id = ?", (row["skill_review_id"],))
    if review is None:
        return CardMessages(f"🧩 {b('Skill')} {esc(row['skill'])}: no longer staged.")
    files = json.loads(review["files_json"])
    tools = json.loads(review["declared_tools_json"]) or ["none"]
    lines = [
        f"🧩 {b('Skill')} {esc(review['skill'])} · v{review['version']} · {esc(review['action'])}",
        f"Hermes asks to {'remove' if review['action'] == 'delete' else 'activate'} a self-written skill"
        f" (tier {esc(review['tier'])}; dry-run n/a: local change).",
        f"Summary: {esc(redact(review['summary'] or '')[0][:300]) or '—'}",
        f"Declared tools: {esc(', '.join(tools))} (the tier, not the skill, decides the tools)",
        f"Files: {esc(', '.join(sorted(files)) or 'none')}",
        f"Digest: {esc(review['content_sha256'][:16])}…",
    ]
    diff = redact(review["diff"] or "")[0] or "(no textual change)"
    parts = [f"<pre>{esc(diff[i : i + PART_CHARS])}</pre>" for i in range(0, len(diff), PART_CHARS)][:MAX_PARTS]
    if len(diff) > PART_CHARS * MAX_PARTS:
        parts[-1] += "\n(diff truncated)"
    return CardMessages("\n".join(lines), parts)


class SkillReviews:
    def __init__(
        self,
        conn: sqlite3.Connection,
        paths: Paths,
        clock: Clock,
        audit: AuditLog,
        skills: SkillApprovals,
        tier: Callable[[], HermesTier],
        chat_id: Callable[[], int],
        on_card: Callable[[str], None] | None = None,
    ) -> None:
        self.conn, self.paths, self.clock, self.audit = conn, paths, clock, audit
        self.skills, self.tier, self.chat_id = skills, tier, chat_id
        self.on_card: Callable[[str], None] = on_card or (lambda approval_id: None)

    @property
    def store(self) -> Path:
        return self.paths.var_lib / "skills"

    def _parts(self, review_id: str) -> int:
        row = db.one(self.conn, "SELECT * FROM skill_reviews WHERE id = ?", (review_id,))
        diff = (row["diff"] if row is not None else "") or "x"
        return max(1, min(MAX_PARTS, -(-len(diff) // PART_CHARS)))

    # ------------------------------------------------------------------------------ import
    def sync(self) -> list[str]:
        """Import decisions, then the pending index; returns the approvals created (cards to publish)."""
        self._import_decisions()
        return self._import_pending()

    def _import_decisions(self) -> None:
        doc = _read_json(self.store / "decisions.json")
        items = doc.get("decisions") if isinstance(doc, dict) else None
        for raw in items if isinstance(items, list) else []:
            try:
                d = Decision.model_validate(raw)
            except ValidationError:
                continue
            if d.review_id is None:
                continue
            status = {"activated": "activated", "rejected": "rejected", "failed": "invalid"}[d.result]
            with db.transaction(self.conn):
                cur = self.conn.execute(
                    "UPDATE skill_reviews SET status = ?, reason = ?, decided_at = ? WHERE id = ? AND status IN"
                    " ('staged','activating')",
                    (status, (d.reason or d.result)[:200], to_iso(self.clock.now()), d.review_id),
                )
                if cur.rowcount:
                    self.audit.append("root:skills", f"skill_{status}", {"review_id": d.review_id, "reason": d.reason})

    def _import_pending(self) -> list[str]:
        doc = _read_json(self.store / "pending.json")
        if not isinstance(doc, dict) or not isinstance(doc.get("reviews"), list):
            return []
        created: list[str] = []
        listed: set[str] = set()
        for raw in doc["reviews"][:50]:
            try:
                m = Manifest.model_validate(raw)
            except ValidationError as exc:
                log.warning("skill manifest refused: %s error(s)", exc.error_count(), extra={"event": "skill_refused"})
                continue
            listed.add(m.review_id)
            approval = self._import_one(m)
            if approval is not None:
                created.append(approval)
        # a staged review whose record Hermes withdrew (or root dropped) is no longer approvable
        for row in self.conn.execute("SELECT id, skill FROM skill_reviews WHERE status = 'staged'").fetchall():
            if row["id"] not in listed:
                self._close(str(row["id"]), str(row["skill"]), "superseded", "withdrawn")
        for approval_id in created:
            self.on_card(approval_id)
        return created

    def _import_one(self, m: Manifest) -> str | None:
        now = to_iso(self.clock.now())
        with db.transaction(self.conn):
            if db.one(self.conn, "SELECT 1 FROM skill_reviews WHERE id = ?", (m.review_id,)):
                return None
            count = self.conn.execute(
                "SELECT COUNT(*) FROM skill_reviews WHERE skill = ? AND status = 'activated'", (m.skill,)
            ).fetchone()
            version = 1 + int(count[0])
            files_json = json.dumps(m.files, sort_keys=True, separators=(",", ":"))
            self.conn.execute(
                "INSERT INTO skill_reviews (id, pending_id, skill, rel_dir, action, version, files_json,"
                " content_sha256, declared_tools_json, tier, summary, diff, origin, status, created_at) VALUES"
                " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'staged', ?)",
                (
                    m.review_id,
                    m.pending_id,
                    m.skill,
                    m.rel_dir,
                    m.action,
                    version,
                    files_json,
                    m.content_sha256,
                    json.dumps(sorted(m.declared_tools)),
                    m.tier,
                    m.summary,
                    m.diff,
                    m.origin,
                    now,
                ),
            )
            self.audit.append(
                "root:skills",
                "skill_staged",
                {"review_id": m.review_id, "skill": m.skill, "action": m.action, "tier": m.tier},
            )
        for row in self.conn.execute(
            "SELECT id FROM skill_reviews WHERE skill = ? AND status = 'staged' AND id != ?", (m.skill, m.review_id)
        ).fetchall():
            self._close(str(row["id"]), m.skill, "superseded", "newer version staged")
        chat = self.chat_id()
        if chat == 0:
            return None
        try:
            return self.skills.create(
                m.review_id, m.skill, version, tg_chat_id=chat, body_parts_total=self._parts(m.review_id)
            )
        except ApprovalError as exc:  # staged under another tier, tools above the tier: stays staged, no card
            log.warning("skill %s not offered: %s", m.skill, exc.code, extra={"event": "skill_not_offered"})
            return None

    def _close(self, review_id: str, skill: str, status: str, reason: str) -> None:
        with db.transaction(self.conn):
            self.conn.execute(
                "UPDATE skill_reviews SET status = ?, reason = ?, decided_at = ? WHERE id = ?",
                (status, reason, to_iso(self.clock.now()), review_id),
            )
            self.skills._invalidate(
                "skill_review_id = ? AND status IN ('pending','armed')", (review_id,), "superseded", "system"
            )

    # ------------------------------------------------------------------------- decisions
    def _request(self, op: str, review_id: str, approval_id: str, content_sha256: str | None) -> None:
        payload: dict[str, Any] = {
            "op": op,
            "review_id": review_id,
            "approval_id": approval_id,
            "ts": to_iso(self.clock.now()),
        }
        if content_sha256 is not None:
            payload["content_sha256"] = content_sha256
        write_flag(self.paths.control_dir / f"skill-request-{approval_id}.json", payload)

    def request_activation(self, approval_id: str, actor: str) -> dict[str, Any]:
        """After the Confirm: ``consume`` re-checks the binding right before the effect, then root is asked
        to activate exactly the bound digest."""
        binding = self.skills.consume(approval_id)
        review_id = str(binding["skill_review_id"])
        with db.transaction(self.conn):
            self.conn.execute(
                "UPDATE skill_reviews SET status = 'activating' WHERE id = ? AND status = 'staged'", (review_id,)
            )
            self.audit.append(
                actor,
                "skill_activation_requested",
                {"approval_id": approval_id, "review_id": review_id, "content_sha256": binding["content_sha256"]},
            )
        self._request("activate", review_id, approval_id, str(binding["content_sha256"]))
        return binding

    def request_rejection(self, approval_id: str, actor: str) -> None:
        row = db.one(self.conn, "SELECT skill_review_id FROM skill_approvals WHERE id = ?", (approval_id,))
        if row is None:
            raise ApprovalError("unknown_approval", approval_id)
        review_id = str(row["skill_review_id"])
        with db.transaction(self.conn):
            self.conn.execute(
                "UPDATE skill_reviews SET status = 'rejected', decided_at = ?, reason = 'owner' WHERE id = ?"
                " AND status = 'staged'",
                (to_iso(self.clock.now()), review_id),
            )
            self.audit.append(actor, "skill_rejected", {"approval_id": approval_id, "review_id": review_id})
        self._request("reject", review_id, approval_id, None)


def staged_count(conn: sqlite3.Connection) -> Mapping[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM skill_reviews GROUP BY status").fetchall()
    return {str(r["status"]): int(r["n"]) for r in rows}
